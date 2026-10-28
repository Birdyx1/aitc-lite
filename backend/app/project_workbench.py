"""项目工作台只读快照，不调用模型、连接外部服务或创建业务记录。"""
from fastapi import APIRouter,Depends
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session

from .agents.tools import overview,NoArgs
from .deps import get_session
from .features import content_hash
from .models import (Requirement,FeatureSheet,Feature,CaseDraft,CaseReview,CaseGeneration,GenerationTask,VectorBuild,
    GenerationSettings,EmbeddingSettings,RerankSettings,JudgeSettings,McpBinding,McpServer,
    ExecutionTask,EvaluationSample,EvaluationRun)

router=APIRouter(prefix='/api/projects/{project_id}/workbench',tags=['项目工作台'])


def model_status(row,source=None):
    ready=bool(row and (row.mock_mode or (row.base_url and row.model)))
    return {'saved':row is not None,'ready':ready,'mode':('mock' if row.mock_mode else 'real') if ready else 'unconfigured',
            'source':source,'note':'仅检查已保存字段，未验证网络、凭据权限或额度。'}


def workbench(session,pid):
    data=overview(session,pid,NoArgs());base=f'/projects/{pid}'
    def count(model,*where):return session.scalar(select(func.count()).select_from(model).where(*where)) or 0
    def grouped(model,field,*where):return dict(session.execute(select(field,func.count()).select_from(model).where(*where).group_by(field)).all())
    requirements=session.scalars(select(Requirement).where(Requirement.project_id==pid).order_by(Requirement.id)).all()
    confirmed=[]
    for req in requirements:
        sheet=session.get(FeatureSheet,req.id)
        if sheet and sheet.confirmed and sheet.source_hash==content_hash(req.title,req.content):confirmed.append(req.id)
    effective_features=count(Feature,Feature.requirement_id.in_(confirmed),Feature.included.is_(True)) if confirmed else 0
    data['confirmed_requirements']=len(confirmed)
    data['current_confirmed_features']=effective_features
    req_ids=select(Requirement.id).where(Requirement.project_id==pid)
    data['generation_sources']=grouped(CaseGeneration,CaseGeneration.source,CaseGeneration.requirement_id.in_(req_ids))
    data['generation_tasks']=grouped(GenerationTask,GenerationTask.status,GenerationTask.requirement_id.in_(req_ids))
    data['vector_tasks']=grouped(VectorBuild,VectorBuild.status,VectorBuild.project_id==pid)
    data['evaluation']={'samples':count(EvaluationSample,EvaluationSample.project_id==pid),
        'runs':grouped(EvaluationRun,EvaluationRun.status,EvaluationRun.project_id==pid),
        'mock_runs':count(EvaluationRun,EvaluationRun.project_id==pid,EvaluationRun.config_snapshot['mock_mode'].as_boolean().is_(True)),
        'real_runs':count(EvaluationRun,EvaluationRun.project_id==pid,EvaluationRun.config_snapshot['mock_mode'].as_boolean().is_(False))}
    gen=session.get(GenerationSettings,1);embedding=session.get(EmbeddingSettings,1);rerank=session.get(RerankSettings,1);judge=session.get(JudgeSettings,1)
    data['configuration']={'generation':model_status(gen),'embedding':model_status(embedding),'rerank':model_status(rerank),
        'judge':model_status(gen,'generation') if not judge or judge.use_generation_model else model_status(judge,'independent')}
    bindings=session.scalars(select(McpBinding).where(McpBinding.project_id==pid)).all()
    valid_bindings=0;enabled_tools=0
    for binding in bindings:
        server=session.get(McpServer,binding.server_id)
        if not server or not server.enabled or binding.server_revision!=server.revision:continue
        valid_bindings+=1;names={tool['name'] for tool in server.tools}
        enabled_tools+=sum(bool(grant.get('enabled')) for name,grant in binding.tools.items() if name in names)
    data['mcp']={'bindings':len(bindings),'current_bindings':valid_bindings,'enabled_tools':enabled_tools,
                 'note':'仅统计当前项目有效授权；未启动进程、发现工具或验证远端连接。'}
    links={key:base+'/'+path for key,path in [('requirements','requirements'),('knowledge','knowledge'),('cases','testcases'),('execution','execution-tasks'),('evaluation','evaluations'),('mcp','mcp')]}
    def action(code,title,detail,path):return {'code':code,'title':title,'detail':detail,'path':path}
    active_generation=session.scalar(select(GenerationTask).where(GenerationTask.requirement_id.in_(req_ids),GenerationTask.status.in_(('queued','running','paused')))
        .order_by(GenerationTask.created_at).limit(1))
    if not requirements:
        next_action=action('requirement','录入第一条需求','当前项目还没有需求。请提供真实需求原文，再进行功能点拆解与确认。',links['requirements'])
    elif active_generation:
        next_action=action('generation_task','查看未完成的生成任务','先核对当前任务进度；暂停任务需在生成页主动恢复，不会在工作台自动执行。',base+f'/requirements/{active_generation.requirement_id}/cases')
    elif len(confirmed)<len(requirements):
        req=next(req for req in requirements if req.id not in confirmed)
        next_action=action('confirmation','核对并确认功能点','至少一条需求尚未有效确认范围，或原文变化使旧确认失效。',base+f'/requirements/{req.id}/features')
    elif data['review_counts'].get('pending',0):
        rid=session.scalar(select(Requirement.id).join(CaseGeneration,CaseGeneration.requirement_id==Requirement.id)
            .join(CaseDraft,CaseDraft.generation_id==CaseGeneration.id).outerjoin(CaseReview,CaseReview.draft_id==CaseDraft.id)
            .where(Requirement.project_id==pid,func.coalesce(CaseReview.status,'pending')=='pending').order_by(CaseGeneration.created_at.desc()).limit(1))
        next_action=action('review','评审待处理草稿',f"共有 {data['review_counts']['pending']} 条待评审；进入生成页并选择需要核对的历史批次。",base+f'/requirements/{rid}/cases')
    elif effective_features>data['current_features_with_generation'] or not data['generation_batches']:
        next_action=(action('generation','生成用例草稿','已确认功能点还需要对应当前版本的草稿；进入需求生成页选择范围和策略。',links['requirements'])
                     if data['configuration']['generation']['ready'] else action('configuration','完善生成模型配置','生成所需字段尚未保存完整；可保存真实模型配置，或显式选择 Mock。','/settings'))
    elif not data['review_counts'].get('accepted',0):
        next_action=action('accepted','准备已采纳用例','当前没有已采纳用例。请核对已完成评审的结论，必要时重新生成；工作台不会自动采纳。',links['requirements'])
    elif not data['execution']['task_counts']:
        next_action=action('execution','创建人工执行任务','从已采纳用例保存执行快照，在实际环境操作后填写结果。',links['execution'])
    elif data['execution']['task_counts'].get('open',0):
        task=session.scalar(select(ExecutionTask).where(ExecutionTask.project_id==pid,ExecutionTask.status=='open').order_by(ExecutionTask.created_at).limit(1))
        next_action=action('execution_open','继续记录人工执行结果','存在尚未结束的人工任务。已记录不等于全部通过，结束前应核对失败与阻塞。',links['execution']+'/'+task.id)
    elif not data['evaluation']['samples']:
        next_action=action('evaluation_sample','维护独立评测样本','正式用例与评测样本相互隔离。录入需求、功能点及人工标准点后，再选择评测范围。',links['evaluation'])
    elif not data['evaluation']['runs']:
        next_action=action('evaluation_run','准备一次小规模评测','先用本次 Mock 验证流程，再主动选择是否进行真实评测；工作台不调用模型。',links['evaluation'])
    else:
        active=session.scalar(select(EvaluationRun).where(EvaluationRun.project_id==pid,EvaluationRun.status.in_(('queued','running','stopping'))).order_by(EvaluationRun.created_at).limit(1))
        if active:next_action=action('evaluation_active','查看当前评测进度','运行结束只表示队列处理完成，仍须核对生成和 Judge 完成率及结果。',links['evaluation']+'/runs/'+active.id)
        elif not data['evaluation']['real_runs']:next_action=action('evaluation_mock','核对 Mock 评测与真实验收准备','当前仅有 Mock 评测记录，它们不证明真实模型质量。准备好样本后可主动选择小规模真实运行。',links['evaluation'])
        else:next_action=action('results','核对本轮验收结果','已有真实运行记录不代表全部成功或质量合格。请查看样本明细、配置差异与人工执行结果。',links['evaluation'])
    data['next_action']=next_action
    data['notes']=['所有数量来自当前项目的本地记录；不自动生成需求、创建任务或调用外部服务。',
        '功能点生成数量、Judge 分数、人工通过率和评测完成数分别代表不同范围，不可相互代替。',
        '配置状态只代表字段已保存，连接、权限、额度和真实业务质量仍需单独验证。']
    return data


@router.get('')
def get_workbench(project_id:int,session:Session=Depends(get_session)):
    # 同一短读快照，避免数量和下一步来自不同写入时刻；返回前释放读锁。
    try:
        session.execute(text('BEGIN'))
        return workbench(session,project_id)
    finally:session.rollback()
