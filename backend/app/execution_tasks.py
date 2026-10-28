"""人工执行管理。任务包含固定采纳用例快照，不启动模型、MCP 或自动化脚本。"""
import hashlib
import json
from datetime import timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .case_generations import summary
from .deps import get_session
from .models import (CaseDraft, CaseGeneration, CaseReview, Requirement, ExecutionTask,
                     ExecutionItem, ExecutionResultEvent, utc_now)
from .projects import require_project
from .reviews import review_output

router=APIRouter(prefix='/api/projects/{project_id}/execution-tasks',tags=['人工测试执行'])
STATUSES=('not_run','passed','failed','blocked')


def valid_text(value):
    if any(ord(c)==0 or 0xD800<=ord(c)<=0xDFFF for c in value):raise ValueError('invalid text')
    return value.strip()


class TaskInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    task_id:UUID
    name:str=Field(min_length=1,max_length=100)
    environment:str=Field(min_length=1,max_length=500)
    executor:str=Field(default='',max_length=80)
    note:str=Field(default='',max_length=2000)
    case_ids:list[UUID]=Field(min_length=1,max_length=200)
    acknowledge_outdated:bool=Field(default=False,strict=True)

    @field_validator('name','environment','executor','note')
    @classmethod
    def text(cls,value):return valid_text(value)

    @model_validator(mode='after')
    def valid(self):
        if not self.name or not self.environment:raise ValueError('required')
        if len(set(self.case_ids))!=len(self.case_ids):raise ValueError('duplicate cases')
        return self


class ResultInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:UUID
    expected_revision:int=Field(ge=0,strict=True)
    status:Literal['not_run','passed','failed','blocked']
    actual_result:str=Field(default='',max_length=5000)
    defect_note:str=Field(default='',max_length=2000)

    @field_validator('actual_result','defect_note')
    @classmethod
    def text(cls,value):return valid_text(value)

    @model_validator(mode='after')
    def valid(self):
        if self.status!='not_run' and not self.actual_result:raise ValueError('actual result required')
        if self.status=='not_run' and (self.actual_result or self.defect_note):raise ValueError('reset requires empty result')
        return self


class CompleteInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(ge=0,strict=True)


def fingerprint(data):return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def iso(value):return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def require_task(session,pid,id):
    require_project(session,pid)
    row=session.get(ExecutionTask,str(id))
    if not row or row.project_id!=pid:raise HTTPException(404,'当前项目下的测试任务不存在。')
    return row


def require_item(session,task,id):
    item=session.get(ExecutionItem,str(id))
    if not item or item.task_id!=task.id:raise HTTPException(404,'该任务下的执行条目不存在。')
    return item


def counts_for(session,task_ids):
    result={id:{key:0 for key in STATUSES} for id in task_ids}
    if task_ids:
        for id,status,count in session.execute(select(ExecutionItem.task_id,ExecutionItem.status,func.count())
                .where(ExecutionItem.task_id.in_(task_ids)).group_by(ExecutionItem.task_id,ExecutionItem.status)):
            result[id][status]=count
    return result


def statistics(counts):
    total=sum(counts.values());decisive=counts['passed']+counts['failed']
    return {**counts,'total':total,'recorded':total-counts['not_run'],
            'pass_rate':round(counts['passed']/decisive*100,2) if decisive else None,
            'pass_rate_denominator':decisive}


def task_output(task,counts):
    return {key:getattr(task,key) for key in ('id','project_id','name','environment','executor','note','status','revision')} | {
        'created_at':iso(task.created_at),'updated_at':iso(task.updated_at),'completed_at':iso(task.completed_at),
        'stats':statistics(counts),'recording_mode':'manual'}


def item_output(item):
    return {key:getattr(item,key) for key in ('id','task_id','position','snapshot','status','actual_result','defect_note','revision')} | {'recorded_at':iso(item.recorded_at)}


def task_detail(session,task):
    items=session.scalars(select(ExecutionItem).where(ExecutionItem.task_id==task.id).order_by(ExecutionItem.position)).all()
    counts={key:0 for key in STATUSES}
    for item in items:counts[item.status]+=1
    return {**task_output(task,counts),'items':[item_output(item) for item in items]}


@router.post('',status_code=201)
def create_task(project_id:int,payload:TaskInput,response:Response,session:Session=Depends(get_session)):
    try:
        session.execute(text('BEGIN IMMEDIATE'));require_project(session,project_id)
        signature=fingerprint(payload.model_dump(mode='json',exclude={'task_id'}))
        existing=session.get(ExecutionTask,str(payload.task_id))
        if existing:
            if existing.project_id!=project_id or existing.request_fingerprint!=signature:raise HTTPException(409,'任务请求标识已被使用，请重新创建。')
            response.status_code=200;return task_detail(session,existing)
        ids=[str(id) for id in payload.case_ids]
        rows=session.execute(select(CaseDraft,CaseReview,CaseGeneration,Requirement)
            .join(CaseReview,CaseReview.draft_id==CaseDraft.id)
            .join(CaseGeneration,CaseGeneration.id==CaseDraft.generation_id)
            .join(Requirement,Requirement.id==CaseGeneration.requirement_id)
            .where(Requirement.project_id==project_id,CaseReview.status=='accepted',CaseDraft.id.in_(ids))).all()
        if len(rows)!=len(ids):raise HTTPException(400,'所选用例含未采纳、已删除或其他项目条目，请刷新用例库。')
        snapshots={};outdated_cache={}
        for draft,review,generation,requirement in rows:
            if generation.id not in outdated_cache:outdated_cache[generation.id]=summary(session,requirement,generation).outdated
            outdated=outdated_cache[generation.id]
            if outdated and not payload.acknowledge_outdated:raise HTTPException(409,'所选用例有生成依据已变化的条目，请核对适用性并明确确认后再创建。')
            snapshots[draft.id]={'case_id':draft.id,'generation_id':generation.id,'requirement_id':requirement.id,
                'requirement_title':generation.requirement_snapshot['title'],'module':generation.feature_snapshot['module'],
                'feature_name':generation.feature_name,'source':generation.source,'was_edited':review.was_edited,
                'review_revision':review.revision,'outdated_at_creation':outdated,
                'fields':review_output(draft,review).current.model_dump()}
        task=ExecutionTask(id=str(payload.task_id),project_id=project_id,request_fingerprint=signature,
            name=payload.name,environment=payload.environment,executor=payload.executor,note=payload.note)
        session.add(task);session.flush()
        for index,id in enumerate(ids):session.add(ExecutionItem(id=str(uuid4()),task_id=task.id,position=index+1,snapshot=snapshots[id]))
        session.flush();result=task_detail(session,task);session.commit();return result
    finally:
        session.rollback()


@router.get('')
def list_tasks(project_id:int,limit:int=Query(default=20,ge=1,le=50),offset:int=Query(default=0,ge=0),session:Session=Depends(get_session)):
    require_project(session,project_id)
    query=select(ExecutionTask).where(ExecutionTask.project_id==project_id)
    tasks=session.scalars(query.order_by(ExecutionTask.created_at.desc(),ExecutionTask.id.desc()).offset(offset).limit(limit)).all()
    counts=counts_for(session,[task.id for task in tasks])
    return {'total':session.scalar(select(func.count()).select_from(ExecutionTask).where(ExecutionTask.project_id==project_id)),
            'items':[task_output(task,counts[task.id]) for task in tasks]}


@router.get('/{task_id}')
def get_task(project_id:int,task_id:UUID,session:Session=Depends(get_session)):
    return task_detail(session,require_task(session,project_id,task_id))


@router.put('/{task_id}/items/{item_id}')
def record_result(project_id:int,task_id:UUID,item_id:UUID,payload:ResultInput,session:Session=Depends(get_session)):
    try:
        session.execute(text('BEGIN IMMEDIATE'));task=require_task(session,project_id,task_id);item=require_item(session,task,item_id)
        signature=fingerprint({'item_id':item.id,**payload.model_dump(mode='json',exclude={'request_id'})})
        previous=session.get(ExecutionResultEvent,str(payload.request_id))
        if previous:
            if previous.item_id!=item.id or previous.request_fingerprint!=signature:raise HTTPException(409,'结果请求标识已被使用，请刷新核对。')
            return {'item':item_output(item),'task_revision':task.revision,'replayed':True}
        if task.status!='open':raise HTTPException(409,'测试任务已结束，不能再修改结果。')
        if item.revision!=payload.expected_revision:raise HTTPException(409,'该条目已在其他页面修改，请刷新核对；当前输入未保存。')
        if (item.status,item.actual_result,item.defect_note)==(payload.status,payload.actual_result,payload.defect_note):
            raise HTTPException(400,'本次结果与已保存内容相同，无需重复保存。')
        item.status=payload.status;item.actual_result=payload.actual_result;item.defect_note=payload.defect_note
        item.revision+=1;item.recorded_at=utc_now();task.revision+=1;task.updated_at=utc_now()
        session.add(ExecutionResultEvent(id=str(payload.request_id),item_id=item.id,request_fingerprint=signature,revision=item.revision,
            status=item.status,actual_result=item.actual_result,defect_note=item.defect_note))
        session.flush();result={'item':item_output(item),'task_revision':task.revision,'replayed':False};session.commit();return result
    finally:
        session.rollback()


@router.get('/{task_id}/items/{item_id}/history')
def result_history(project_id:int,task_id:UUID,item_id:UUID,session:Session=Depends(get_session)):
    task=require_task(session,project_id,task_id);require_item(session,task,item_id)
    rows=session.scalars(select(ExecutionResultEvent).where(ExecutionResultEvent.item_id==str(item_id)).order_by(ExecutionResultEvent.revision.desc())).all()
    return [{key:getattr(row,key) for key in ('id','revision','status','actual_result','defect_note')} | {'created_at':iso(row.created_at)} for row in rows]


@router.post('/{task_id}/complete')
def complete_task(project_id:int,task_id:UUID,payload:CompleteInput,session:Session=Depends(get_session)):
    try:
        session.execute(text('BEGIN IMMEDIATE'));task=require_task(session,project_id,task_id)
        if task.status=='completed':return task_detail(session,task)
        if task.revision!=payload.expected_revision:raise HTTPException(409,'任务结果已变化，请刷新后再结束任务。')
        if session.scalar(select(ExecutionItem.id).where(ExecutionItem.task_id==task.id,ExecutionItem.status=='not_run').limit(1)):
            raise HTTPException(409,'仍有未执行条目，请记录结果或阻塞原因后再结束。')
        task.status='completed';task.revision+=1;task.completed_at=task.updated_at=utc_now()
        result=task_detail(session,task);session.commit();return result
    finally:
        session.rollback()


def project_execution_summary(session,project_id):
    tasks=select(ExecutionTask.id).where(ExecutionTask.project_id==project_id)
    counts={key:0 for key in STATUSES}
    for status,count in session.execute(select(ExecutionItem.status,func.count()).where(ExecutionItem.task_id.in_(tasks)).group_by(ExecutionItem.status)):
        counts[status]=count
    task_counts=dict(session.execute(select(ExecutionTask.status,func.count()).where(ExecutionTask.project_id==project_id).group_by(ExecutionTask.status)).all())
    recent=session.scalars(select(ExecutionTask).where(ExecutionTask.project_id==project_id).order_by(ExecutionTask.created_at.desc(),ExecutionTask.id.desc()).limit(5)).all()
    task_stats=counts_for(session,[task.id for task in recent])
    return {'available':True,'recording_mode':'manual','task_counts':task_counts,'item_stats':statistics(counts),
            'recent_tasks':[task_output(task,task_stats[task.id]) for task in recent],
            'note':'按人工记录的任务条目统计，同一用例在不同任务中分别计数；通过率=通过/(通过+失败)，不含阻塞和未执行，非自动化实测证明。'}
