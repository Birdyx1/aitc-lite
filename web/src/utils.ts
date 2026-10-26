export function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

export function errorMessage(cause: unknown) {
  return cause instanceof Error ? cause.message : '操作失败，请稍后重试。'
}
