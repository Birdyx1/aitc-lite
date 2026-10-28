import json
from uuid import UUID,uuid4
from fastapi import APIRouter,Depends,HTTPException,Query,Request,Response
from sqlalchemy import select,text,func
from sqlalchemy.orm import Session

from ..agent_sessions import sanitize,secrets
from ..deps import get_session
from ..execution_tasks import iso
from ..models import EvaluationSample,EvaluationRun,EvaluationResult
from ..projects import require_project
from ..models import utc_now
from .schemas import SampleInput,RunInput
from .runtime import configuration,baseline,digest,ENGINE_VERSION,ACTIVE
from .metrics import metrics

router=APIRouter(prefix='/api/projects/{project_id}/evaluations',tags=['离线评测'])


def require_sample(session,pid,id):
    require_project(session,pid);row=session.get(EvaluationSample,str(id))
    if not row or row.project_id!=pid:raise HTTPException(404,'当前项目的评测样本不存在。')
    return row


def sample_output(row):return {key:getattr(row,key) for key in ('id','project_id','name','revision','content','content_hash')} | {'updated_at':iso(row.updated_at)}


def content(payload,id):
    return {'requirement':{'title':payload.requirement_title,'content':payload.requirement_content},
        'feature':{**payload.feature.model_dump(mode='json'),'id':id,'included':True},'gold_points':payload.gold_points}


@router.get('/samples')
def list_samples(project_id:int,session:Session=Depends(get_session)):
    require_project(session,project_id)
    return [sample_output(row) for row in session.scalars(select(EvaluationSample).where(EvaluationSample.project_id==project_id).order_by(EvaluationSample.updated_at.desc(),EvaluationSample.id))]


@router.post('/samples',status_code=201)
def add_sample(project_id:int,payload:SampleInput,session:Session=Depends(get_session)):
    try:
        session.execute(text('BEGIN IMMEDIATE'));require_project(session,project_id)
        if session.scalar(select(func.count()).select_from(EvaluationSample).where(EvaluationSample.project_id==project_id))>=200:raise HTTPException(400,'本项目最多 200 个评测样本。')
        id=str(uuid4());body=sanitize(content(payload,id),secrets(session))
        row=EvaluationSample(id=id,project_id=project_id,name=sanitize(payload.name,secrets(session)),content=body,content_hash=digest(body))
        session.add(row);session.flush();result=sample_output(row);session.commit();return result
    finally:session.rollback()


@router.put('/samples/{sample_id}')
def edit_sample(project_id:int,sample_id:UUID,payload:SampleInput,session:Session=Depends(get_session)):
    try:
        session.execute(text('BEGIN IMMEDIATE'));row=require_sample(session,project_id,sample_id)
        if row.revision!=payload.expected_revision:raise HTTPException(409,'样本已变化，请刷新后编辑；输入未保存。')
        row.name=sanitize(payload.name,secrets(session));row.content=sanitize(content(payload,row.id),secrets(session));row.content_hash=digest(row.content)
        row.revision+=1;row.updated_at=utc_now();result=sample_output(row);session.commit();return result
    finally:session.rollback()


@router.delete('/samples/{sample_id}',status_code=204)
def delete_sample(project_id:int,sample_id:UUID,expected_revision:int=Query(ge=1),session:Session=Depends(get_session)):
    try:
        session.execute(text('BEGIN IMMEDIATE'));row=require_sample(session,project_id,sample_id)
        if row.revision!=expected_revision:raise HTTPException(409,'样本已变化，请刷新后删除。')
        session.delete(row);session.commit();return Response(status_code=204)
    finally:session.rollback()


def require_run(session,pid,id):
    require_project(session,pid);row=session.get(EvaluationRun,str(id))
    if not row or row.project_id!=pid:raise HTTPException(404,'当前项目的评测运行不存在。')
    return row


def result_output(row,detail=False):
    output={key:getattr(row,key) for key in ('id','position','sample_id','sample_snapshot','status','generation_status','judge_status','error_message','model_call_pending','timings','usage')}
    output['case_count']=len(row.cases)
    if detail:output.update({key:getattr(row,key) for key in ('knowledge','messages','cases','judge_input','judge_report','matches','duplicate_findings')})
    return output


def results(session,run):return session.scalars(select(EvaluationResult).where(EvaluationResult.run_id==run.id).order_by(EvaluationResult.position)).all()


def run_output(session,run,detail=False):
    rows=results(session,run)
    output={key:getattr(run,key) for key in ('id','project_id','name','status','error_message','config_snapshot')}
    output.update({'created_at':iso(run.created_at),'updated_at':iso(run.updated_at),'metrics':metrics(rows),
                   'finished_count':sum(row.status not in ACTIVE for row in rows),'sample_count':len(rows)})
    if detail:output['results']=[result_output(row) for row in rows]
    return output


@router.post('/runs',status_code=202)
def submit_run(project_id:int,payload:RunInput,response:Response,session:Session=Depends(get_session)):
    try:
        session.execute(text('BEGIN IMMEDIATE'));require_project(session,project_id)
        signature=digest(payload.model_dump(mode='json',exclude={'run_id'}));existing=session.get(EvaluationRun,str(payload.run_id))
        if existing:
            if existing.project_id!=project_id or existing.request_fingerprint!=signature:raise HTTPException(409,'运行请求标识已被使用，请重新提交。')
            response.status_code=200;return run_output(session,existing,True)
        if session.scalar(select(EvaluationRun.id).where(EvaluationRun.project_id==project_id,EvaluationRun.status.in_(ACTIVE)).limit(1)):
            raise HTTPException(409,'本项目已有活动评测运行，请等待结束。')
        snapshots=[]
        for id in payload.sample_ids:
            sample=require_sample(session,project_id,id)
            snapshot=sanitize(sample_output(sample),secrets(session))
            snapshot['content_hash']=digest(snapshot['content'])
            snapshots.append(snapshot)
        config=configuration(session,project_id,payload)
        if len(json.dumps([config,snapshots],ensure_ascii=False).encode())>4*1024*1024:raise HTTPException(400,'评测快照超过 4 MiB，请缩小样本范围。')
        run=EvaluationRun(id=str(payload.run_id),project_id=project_id,name=sanitize(payload.name,secrets(session)),request_fingerprint=signature,
            config_snapshot=config,config_signature=baseline(session,payload.use_rag),engine_version=ENGINE_VERSION)
        session.add(run);session.flush()
        for i,snapshot in enumerate(snapshots):session.add(EvaluationResult(id=str(uuid4()),run_id=run.id,position=i+1,sample_id=snapshot['id'],sample_snapshot=snapshot))
        session.flush();result=run_output(session,run,True);session.commit();return result
    finally:session.rollback()


@router.get('/runs')
def list_runs(project_id:int,offset:int=Query(default=0,ge=0),limit:int=Query(default=20,ge=1,le=50),session:Session=Depends(get_session)):
    require_project(session,project_id)
    rows=session.scalars(select(EvaluationRun).where(EvaluationRun.project_id==project_id).order_by(EvaluationRun.created_at.desc(),EvaluationRun.id.desc()).offset(offset).limit(limit))
    return {'total':session.scalar(select(func.count()).select_from(EvaluationRun).where(EvaluationRun.project_id==project_id)),
            'items':[run_output(session,row) for row in rows]}


@router.get('/runs/{run_id}')
def get_run(project_id:int,run_id:UUID,session:Session=Depends(get_session)):return run_output(session,require_run(session,project_id,run_id),True)


@router.get('/runs/{run_id}/results/{result_id}')
def get_result(project_id:int,run_id:UUID,result_id:UUID,session:Session=Depends(get_session)):
    run=require_run(session,project_id,run_id);row=session.get(EvaluationResult,str(result_id))
    if not row or row.run_id!=run.id:raise HTTPException(404,'本次运行的样本结果不存在。')
    return result_output(row,True)


@router.get('/compare')
def compare(project_id:int,left:UUID,right:UUID,session:Session=Depends(get_session)):
    a=require_run(session,project_id,left);b=require_run(session,project_id,right)
    if a.status in ACTIVE or b.status in ACTIVE:raise HTTPException(409,'运行结束或中断后才能对比，避免比较变化中的指标。')
    aa={row.sample_id:row for row in results(session,a)};bb={row.sample_id:row for row in results(session,b)}
    intersection=set(aa)&set(bb)
    same=sorted(id for id in intersection if aa[id].sample_snapshot['content_hash']==bb[id].sample_snapshot['content_hash'])
    changed=sorted(intersection-set(same))
    differences=[]
    for key in sorted(set(a.config_snapshot)|set(b.config_snapshot)):
        if a.config_snapshot.get(key)!=b.config_snapshot.get(key):differences.append({'field':key,'left':a.config_snapshot.get(key),'right':b.config_snapshot.get(key)})
    return {'left':run_output(session,a),'right':run_output(session,b), 'common_count':len(same),
        'common_metrics':{'left':metrics([aa[id] for id in same]),'right':metrics([bb[id] for id in same])},
        'changed_sample_ids':changed,'left_only_ids':sorted(set(aa)-set(bb)),'right_only_ids':sorted(set(bb)-set(aa)),
        'config_differences':differences,'metric_definitions_equal':all(a.config_snapshot.get(key)==b.config_snapshot.get(key) for key in ('match_version','rules_version','rubric_version','metrics_version')),
        'rows':[{'sample_id':id,'name':aa[id].sample_snapshot['name'],'left_status':aa[id].status,'right_status':bb[id].status,
                 'left_metrics':metrics([aa[id]]),'right_metrics':metrics([bb[id]])} for id in same],
        'note':'仅同一 ID 且内容哈希一致的样本参与共同指标；变更样本、单侧样本排除。Mock 与真实、Judge/知识/策略不同需结合配置差异解释，不自动排名。'}


@router.post('/runs/{run_id}/stop')
def stop_run(project_id:int,run_id:UUID,session:Session=Depends(get_session)):
    try:
        session.execute(text('BEGIN IMMEDIATE'));run=require_run(session,project_id,run_id)
        if run.status=='queued':
            run.status='interrupted';run.error_message='用户停止排队运行，未调用模型。'
            for row in results(session,run):row.status='interrupted';row.error_message=run.error_message
        elif run.status=='running':
            run.status='stopping';run.error_message='停止请求已保存，当前调用结束后保留结果，不再开始后续阶段。'
        run.updated_at=utc_now()
        result=run_output(session,run,True);session.commit();return result
    finally:session.rollback()
