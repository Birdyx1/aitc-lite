<script setup lang="ts">
import type {EvaluationMetrics,EvaluationRatio} from '../api'
defineProps<{value:EvaluationMetrics}>()
function show(value:EvaluationRatio){return `${value.percent===null?'—':value.percent+'%'}（${value.numerator}/${value.denominator}）`}
</script>
<template><div class="evaluation-metrics"><span>样本 {{value.sample_count}} · 生成用例 {{value.generated_case_count}}</span><span>生成成功 {{show(value.generation_success)}}</span><span>Judge 完成 {{show(value.judge_completion)}}</span><span>Judge 可用 {{show(value.judge_usable)}}</span><span>词面场景召回 {{show(value.scenario_recall)}}</span><span>重复 {{show(value.duplicate_rate)}}</span><span>疑似幻觉 {{value.suspect_case_count}} 条用例</span><span>阶段耗时合计 {{value.total_elapsed_ms}} ms</span><span>已知生成/Judge Token {{value.token_usage.known_total_tokens??'未知'}} · 缺失 {{value.token_usage.unknown_usage_calls}} 次调用</span><span>费用：未知</span></div></template>
<style scoped>.evaluation-metrics{display:flex;gap:12px 22px;flex-wrap:wrap;line-height:1.9}.evaluation-metrics span{padding:6px 10px;border-radius:6px;background:#f2f6fb}</style>
