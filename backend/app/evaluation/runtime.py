import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select,update

from ..agent_sessions import sanitize,secrets
from ..features import content_hash
from ..generation_tasks import config_signature
from ..settings import load_model_config
from ..judge_settings import resolve_judge_config,selected_transport
from ..judge_protocol import PROMPT as JUDGE_PROMPT,PROMPT_VERSION as JUDGE_PROMPT_VERSION,RUBRIC_VERSION,RULES_VERSION,mock_report,parse_report
from ..judge_runs import local_checks
from ..llm import chat_completion
from ..models import EvaluationRun,EvaluationResult,EmbeddingSettings,RerankSettings,utc_now
from ..specialists.registry import snapshots,assert_supported
from ..workflows.case_generation import (build_case_graph,WORKFLOW_VERSION,PROMPT,PROMPT_VERSION,RAG_PROMPT,RAG_PROMPT_VERSION,
    SKILL_RULES,SKILL_PROMPT_VERSION,RAG_SKILL_PROMPT_VERSION)
from ..hybrid_search import retrieve_hybrid,HybridInput,bm25_recall
from ..knowledge.bm25 import read_chunks,current_signature
from .metrics import match_points,duplicates,MATCH_VERSION


def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
ENGINE_VERSION=hashlib.sha256(Path(__file__).read_bytes()+(Path(__file__).parent/'metrics.py').read_bytes()+WORKFLOW_VERSION.encode()+JUDGE_PROMPT.encode()+(Path(__file__).parents[1]/'llm.py').read_bytes()+(Path(__file__).parents[1]/'judge_protocol.py').read_bytes()).hexdigest()
ACTIVE=('queued','running','stopping')


def baseline(session,use_rag):
    gen=load_model_config(session);judge,source=resolve_judge_config(session)
    inputs=[config_signature(gen),config_signature(judge),source]
    if use_rag:
        for model in (EmbeddingSettings,RerankSettings):
            row=session.get(model,1)
            inputs.append({column.name:getattr(row,column.name) for column in model.__table__.columns} if row else None)
    return digest(inputs)


def public_config(config):
    return {key:getattr(config,key) for key in ('base_url','model','mock_mode','timeout_seconds')}


def configuration(session,pid,payload):
    gen=load_model_config(session);judge,source=resolve_judge_config(session)
    if not payload.mock_mode and (gen.mock_mode or judge.mock_mode):
        raise HTTPException(400,'真实评测要求生成与 Judge 均关闭 Mock；也可显式选择本次 Mock 演示。')
    skills=snapshots(payload.skill_ids)
    prompt_version=RAG_SKILL_PROMPT_VERSION if payload.use_rag and skills else SKILL_PROMPT_VERSION if skills else RAG_PROMPT_VERSION if payload.use_rag else PROMPT_VERSION
    prompt=PROMPT+(SKILL_RULES+'\n'+'\n'.join(item['prompt'] for item in skills) if skills else '')+(RAG_PROMPT if payload.use_rag else '')
    result={**payload.model_dump(mode='json',exclude={'run_id','sample_ids','name'}),
        'generation':public_config(replace(gen,mock_mode=payload.mock_mode)),
        'judge':{**public_config(replace(judge,mock_mode=payload.mock_mode)),'source':source},
        'generation_prompt_version':prompt_version,'generation_prompt':prompt,'judge_prompt_version':JUDGE_PROMPT_VERSION,
        'judge_prompt':JUDGE_PROMPT,'rubric_version':RUBRIC_VERSION,'rules_version':RULES_VERSION,'match_version':MATCH_VERSION,
        'workflow_version':WORKFLOW_VERSION,'evaluation_engine_version':ENGINE_VERSION,
        'metrics_version':hashlib.sha256((Path(__file__).parent/'metrics.py').read_bytes()).hexdigest(),'skills':skills,
        'corpus_signature':current_signature(session,pid) if payload.use_rag else None}
    if payload.use_rag:
        result['retrieval_config']={}
        for name,model in [('embedding',EmbeddingSettings),('rerank',RerankSettings)]:
            row=session.get(model,1)
            result['retrieval_config'][name]={column.name:getattr(row,column.name) for column in model.__table__.columns if column.name!='api_key'} if row else None
        result['retrieval_config']['candidate_k']=20
    return sanitize(result,secrets(session))


def guard(session,run):
    if run and run.status=='stopping':raise HTTPException(409,'用户已停止剩余样本；已保存产物保留，未自动重试。')
    if not run or run.status!='running':raise HTTPException(409,'评测运行已停止。')
    if run.engine_version!=ENGINE_VERSION or run.config_signature!=baseline(session,run.config_snapshot['use_rag']):
        raise HTTPException(409,'模型/检索配置或评测流程已变化，本次停止后续调用；请创建新运行。')
    assert_supported(run.config_snapshot['skills'])
    if run.config_snapshot['use_rag'] and current_signature(session,run.project_id)!=run.config_snapshot['corpus_signature']:
        raise HTTPException(409,'知识库已变化，本次停止后续调用；历史上下文保留，请创建新运行。')


async def knowledge(app,run_id,snapshot):
    with app.state.session_factory() as session:
        run=session.get(EvaluationRun,run_id);guard(session,run);config=run.config_snapshot;pid=run.project_id
        query=' '.join([snapshot['feature']['feature'],snapshot['feature']['description'],snapshot['requirement']['title']])[:200]
        if config['mock_mode']:
            chunks=read_chunks(session,pid);signature=current_signature(session,pid);session.rollback()
            route,ids,terms,details=await asyncio.to_thread(bm25_recall,chunks,query,config['rag_top_k'])
            if route.status=='error':raise HTTPException(503,'Mock 本地知识查询失败，未调用外部服务。')
            by_id={chunk.chunk_id:chunk for chunk in chunks}
            hits=[{'chunk_id':id,'document_id':by_id[id].document_id,'document_title':by_id[id].document_title,
                'document_revision':by_id[id].document_revision,'content':by_id[id].content,'rank':i,'score':details[id][0]} for i,id in enumerate(ids,1)]
            context={'query':query,'sources':hits,'corpus_signature':signature,'mode':'mock_keyword_only','mock_mode':True,
                'degraded':True,'message':'Mock 仅本地 BM25，不调用 Embedding/Rerank。','query_tokens':terms,'rerank':{'status':'disabled','message':'Mock 不执行精排。'}}
        else:
            try:
                result=await retrieve_hybrid(pid,HybridInput(query=query,top_k=config['rag_top_k'],candidate_k=20,use_rerank=config['rag_rerank']),app,session)
                context=result.model_dump(mode='json');context['sources']=context.pop('hits')
            except HTTPException as error:
                if error.status_code!=503:raise
                context={'query':query,'sources':[],'corpus_signature':config['corpus_signature'],'mode':'no_results','degraded':True,
                         'mock_mode':False,'message':'检索服务失败，无有效知识候选。'}
        if not context['sources']:
            context['degraded']=True;context['message']+=(' 本次无知识继续。' if config['rag_allow_empty'] else ' 本次禁止无知识生成。')
        for i,source in enumerate(context['sources'],1):source['citation_id']=f'K{i}'
        context['context_version']='evaluation-rag-v1'
    with app.state.session_factory() as session:guard(session,session.get(EvaluationRun,run_id));return sanitize(context,secrets(session))


def persist(app,id,**values):
    with app.state.session_factory() as session:
        row=session.get(EvaluationResult,id)
        if not row:return
        for key,value in values.items():setattr(row,key,sanitize(value,secrets(session)))
        row.updated_at=utc_now();session.commit()


async def execute_result(app,run_id,result_id):
    with app.state.session_factory() as session:
        run=session.get(EvaluationRun,run_id);guard(session,run);config=deepcopy(run.config_snapshot)
        row=session.get(EvaluationResult,result_id);snapshot=deepcopy(row.sample_snapshot['content'])
        gen=replace(load_model_config(session),mock_mode=config['mock_mode']);judge,source=resolve_judge_config(session);judge=replace(judge,mock_mode=config['mock_mode'])
    timings={};usage={};context=None;generation_started=None
    persist(app,result_id,status='running',generation_status='running')
    try:
        if config['use_rag']:
            at=perf_counter()
            try:context=await knowledge(app,run_id,snapshot)
            finally:timings['retrieval_ms']=round((perf_counter()-at)*1000)
            persist(app,result_id,knowledge=context,timings=timings)
            if not context['sources'] and not config['rag_allow_empty']:raise HTTPException(400,'无知识候选，未调用生成模型；可显式允许无知识继续。')
        async def call_model(messages,max_tokens):
            with app.state.session_factory() as session:guard(session,session.get(EvaluationRun,run_id))
            if len(json.dumps(messages,ensure_ascii=False).encode())>512*1024:raise HTTPException(400,'生成输入超过 512 KiB，未调用模型。')
            usage['generation']=None
            persist(app,result_id,messages=messages,model_call_pending=True,usage=usage)
            reply=await chat_completion(gen,messages,max_tokens,transport=app.state.llm_transport,max_response_bytes=1024*1024,usage_sink=lambda value:usage.update(generation=value))
            persist(app,result_id,model_call_pending=False,usage=usage)
            return reply
        async def provider():return deepcopy(context)
        graph=build_case_graph(call_model,knowledge_provider=provider if context else None,skill_snapshot=config['skills'])
        at=perf_counter();generation_started=at;state=await graph.ainvoke({'requirement':snapshot['requirement'],'feature':snapshot['feature'],'strategy':config['strategy'],'source':'mock' if config['mock_mode'] else 'real'})
        timings['generation_ms']=round((perf_counter()-at)*1000)
        if config['mock_mode']:usage['generation']={'mock':True,'total_tokens':None}
        cases=[{'case_id':str(uuid4()),'fields':fields,'knowledge_refs':state['citations'][i],'skill_refs':state['skill_tags'][i]} for i,fields in enumerate(state['cases'])]
        with app.state.session_factory() as session:
            cases=sanitize(cases,secrets(session));state['messages']=sanitize(state['messages'],secrets(session))
        found=match_points(snapshot['gold_points'],cases);dup=duplicates(cases)
        persist(app,result_id,generation_status='succeeded',cases=cases,messages=state['messages'],matches=found,duplicate_findings=dup,
                model_call_pending=False,timings=timings,usage=usage)
    except Exception as error:
        timings['generation_ms']=round((perf_counter()-generation_started)*1000) if generation_started is not None else 0
        persist(app,result_id,status='failed',generation_status='failed',judge_status='skipped',error_message=error_text(error),timings=timings,usage=usage)
        return
    judge_started=None
    try:
        with app.state.session_factory() as session:guard(session,session.get(EvaluationRun,run_id))
        judge_input={'requirement':snapshot['requirement'],'feature':snapshot['feature'],'strategy':config['strategy'],
            'knowledge':context,'skills':config['skills'],'cases':cases,'rule_findings':local_checks(cases,False),'rules_version':RULES_VERSION,
            'generation_source':'mock' if config['mock_mode'] else 'real','generation_outdated':False}
        if len(json.dumps(judge_input,ensure_ascii=False).encode())>512*1024:raise HTTPException(400,'Judge 输入超过 512 KiB，生成结果保留。')
        usage['judge']={'mock':True,'total_tokens':None} if config['mock_mode'] else None
        persist(app,result_id,judge_input=judge_input,judge_status='running',model_call_pending=not config['mock_mode'],usage=usage)
        at=perf_counter();judge_started=at
        raw=mock_report(judge_input) if config['mock_mode'] else await chat_completion(judge,[{'role':'system','content':config['judge_prompt']},{'role':'user','content':json.dumps(judge_input,ensure_ascii=False)}],6144,
                transport=selected_transport(app,source),max_response_bytes=1024*1024,usage_sink=lambda value:usage.update(judge=value))
        persist(app,result_id,model_call_pending=False,usage=usage)
        report=parse_report(raw,[case['case_id'] for case in cases]);timings['judge_ms']=round((perf_counter()-at)*1000)
        persist(app,result_id,status='succeeded',judge_status='succeeded',judge_report=report,model_call_pending=False,timings=timings,usage=usage)
    except Exception as error:
        timings['judge_ms']=round((perf_counter()-judge_started)*1000) if judge_started is not None else 0
        persist(app,result_id,status='partial',judge_status='failed',error_message=error_text(error),timings=timings,usage=usage)


def error_text(error):return str(error.detail) if isinstance(error,HTTPException) else '评测阶段失败，请核对配置和输入；未自动重试。'


def settle(app):
    with app.state.session_factory() as session:
        ids=select(EvaluationRun.id).where(EvaluationRun.status.in_(ACTIVE))
        session.execute(update(EvaluationResult).where(EvaluationResult.run_id.in_(ids),EvaluationResult.status.in_(ACTIVE)).values(status='interrupted',error_message='服务停止或重启，未自动重放；已保存产物保留。'))
        session.execute(update(EvaluationResult).where(EvaluationResult.run_id.in_(ids),EvaluationResult.generation_status=='running').values(generation_status='interrupted'))
        session.execute(update(EvaluationResult).where(EvaluationResult.run_id.in_(ids),EvaluationResult.judge_status=='running').values(judge_status='interrupted'))
        session.execute(update(EvaluationRun).where(EvaluationRun.status.in_(ACTIVE)).values(status='interrupted',error_message='服务停止或重启，本轮中断；未自动重放模型或检索调用。',updated_at=utc_now()))
        session.commit()


async def worker(app):
    while True:
        try:
            with app.state.session_factory() as session:
                id=session.scalar(select(EvaluationRun.id).where(EvaluationRun.status=='queued').order_by(EvaluationRun.created_at,EvaluationRun.id).limit(1))
                if id:
                    changed=session.execute(update(EvaluationRun).where(EvaluationRun.id==id,EvaluationRun.status=='queued').values(status='running',updated_at=utc_now()));session.commit()
                    if not changed.rowcount:id=None
            if not id:await asyncio.sleep(.25);continue
            try:
                with app.state.session_factory() as session:ids=list(session.scalars(select(EvaluationResult.id).where(EvaluationResult.run_id==id).order_by(EvaluationResult.position)))
                for result_id in ids:
                    with app.state.session_factory() as session:guard(session,session.get(EvaluationRun,id))
                    await execute_result(app,id,result_id)
                with app.state.session_factory() as session:
                    run=session.get(EvaluationRun,id)
                    if run:
                        guard(session,run)
                        run.status='completed';run.updated_at=utc_now();session.commit()
            except asyncio.CancelledError:raise
            except Exception as error:
                with app.state.session_factory() as session:
                    run=session.get(EvaluationRun,id)
                    if run:
                        run.status='interrupted';run.error_message=sanitize(error_text(error),secrets(session));run.updated_at=utc_now()
                        session.execute(update(EvaluationResult).where(EvaluationResult.run_id==id,EvaluationResult.status.in_(ACTIVE)).values(status='interrupted',error_message='本轮停止，未自动重试。'));session.commit()
        except asyncio.CancelledError:raise
        except Exception:await asyncio.sleep(1)
