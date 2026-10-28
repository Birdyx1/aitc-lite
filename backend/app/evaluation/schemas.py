from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from ..execution_tasks import valid_text
from ..schemas import FeatureFields
from ..judge_runs import normalized


class SampleInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=100)
    requirement_title:str=Field(min_length=1,max_length=200)
    requirement_content:str=Field(min_length=1,max_length=50000)
    feature:FeatureFields
    gold_points:list[str]=Field(default_factory=list,max_length=20)
    expected_revision:int=Field(default=0,ge=0,strict=True)

    @field_validator('name','requirement_title','requirement_content')
    @classmethod
    def text(cls,value):
        value=valid_text(value)
        if not value:raise ValueError('empty')
        return value

    @field_validator('gold_points')
    @classmethod
    def points(cls,values):
        points=[valid_text(value) for value in values]
        if any(len(value)<2 or len(value)>500 for value in points) or len(set(map(normalized,points)))!=len(points):raise ValueError('invalid points')
        return points

    @model_validator(mode='after')
    def valid_feature(self):
        for value in self.feature.model_dump().values():
            if isinstance(value,str):valid_text(value)
        return self


class RunInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    run_id:UUID
    name:str=Field(min_length=1,max_length=100)
    sample_ids:list[UUID]=Field(min_length=1,max_length=20)
    strategy:Literal['full','quick']='full'
    skill_ids:list[Literal['api','security']]=Field(default_factory=list,max_length=2)
    mock_mode:bool=Field(default=False,strict=True)
    use_rag:bool=Field(default=False,strict=True)
    rag_top_k:int=Field(default=3,ge=1,le=5,strict=True)
    rag_rerank:bool=Field(default=False,strict=True)
    rag_allow_empty:bool=Field(default=False,strict=True)

    @field_validator('name')
    @classmethod
    def text(cls,value):
        value=valid_text(value)
        if not value:raise ValueError('empty')
        return value

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.sample_ids))!=len(self.sample_ids) or len(set(self.skill_ids))!=len(self.skill_ids):raise ValueError('duplicates')
        if not self.use_rag and (self.rag_rerank or self.rag_allow_empty):raise ValueError('rag off')
        return self
