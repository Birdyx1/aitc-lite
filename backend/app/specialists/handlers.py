"""独立 handler 生成结构化设计关注点，不发网络请求，不执行用户代码。"""


def api_context(feature, strategy):
    return {
        'scope': f"{feature['module']} / {feature['feature']}",
        'contract_source': '当前需求、已确认功能点与本次注入知识，缺失约定必须标待澄清',
        'checks': ['合法请求与响应契约'] if strategy == 'quick' else
                  ['请求/响应契约', '参数类型与边界', '缺失与非法参数', '仅在明确约定时检查幂等/分页/并发'],
    }


def security_context(feature, strategy):
    return {
        'scope': f"{feature['module']} / {feature['feature']}",
        'authorization_source': '只使用明确声明的角色、资源归属与权限规则',
        'checks': ['有权限身份的正常主路径'] if strategy == 'quick' else
                  ['认证与会话状态', '角色及资源归属边界', '拒绝访问后的数据不变性', '已声明的敏感信息保护'],
    }
