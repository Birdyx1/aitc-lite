import json
from ..judge_runs import local_checks,normalized

MATCH_VERSION='literal-field-nfkc-v1'


def match_points(points,cases):
    result=[]
    for point in points:
        target=normalized(point);evidence=[]
        for case in cases:
            fields=case['fields']
            for key,value in [('title',fields['title']),('precondition',fields['precondition']),
                              *[(f'steps[{i}]',v) for i,v in enumerate(fields['steps'])],('expected_result',fields['expected_result'])]:
                text=normalized(value);at=text.find(target)
                if at>=0:
                    evidence.append({'case_id':case['case_id'],'field':key,'normalized_excerpt':text[max(0,at-60):at+len(target)+60]});break
        result.append({'point':point,'matched':bool(evidence),'evidence':evidence,'method':MATCH_VERSION})
    return result


def duplicates(cases):
    return [row for row in local_checks(cases,False) if row['code'].startswith('duplicate_')]


def ratio(n,d):return {'numerator':n,'denominator':d,'percent':round(n/d*100,2) if d else None}


def metrics(results):
    generated=[r for r in results if r.generation_status=='succeeded']
    checked=[r for r in generated if r.judge_status=='succeeded' and r.judge_report]
    cases=sum(len(r.cases) for r in generated);judged=sum(r.judge_report['checked_count'] for r in checked)
    points=sum(len(r.sample_snapshot['content']['gold_points']) for r in results)
    matched=sum(sum(point['matched'] for point in r.matches) for r in generated)
    dup=sum(len({v['case_id'] for v in r.duplicate_findings}) for r in generated)
    usage=[u for r in results for u in r.usage.values() if isinstance(u,dict)]
    totals=[u['total_tokens'] for u in usage if u.get('total_tokens') is not None]
    return {'sample_count':len(results),'generated_case_count':cases,
        'generation_success':ratio(len(generated),len(results)),
        'judge_completion':ratio(judged,cases),'judge_sample_completion':ratio(len(checked),len(generated)),
        'judge_usable':ratio(sum(r.judge_report['usable_count'] for r in checked),judged),
        'scenario_recall':ratio(matched,points),'gold_sample_count':sum(bool(r.sample_snapshot['content']['gold_points']) for r in results),
        'duplicate_rate':ratio(dup,cases),'suspect_case_count':sum(r.judge_report['suspect_count'] for r in checked),
        'total_elapsed_ms':sum(sum(r.timings.values()) for r in results),
        'token_usage':{'known_total_tokens':sum(totals) if totals else None,'calls_with_known_usage':len(totals),
                       'unknown_usage_calls':sum(not isinstance(value,dict) or (value.get('total_tokens') is None and not value.get('mock')) for r in results for value in r.usage.values())},
        'cost':None,'match_method':MATCH_VERSION,
        'notes':['召回为 NFKC/空白/大小写归一化后单字段整段包含匹配，不代表语义覆盖率。',
                 '有标准点但生成失败的样本仍计入召回分母；无标准点不参与召回。',
                 '重复按每个样本内标题或完整步骤/前置/预期相同的后续用例去重计数。']}
