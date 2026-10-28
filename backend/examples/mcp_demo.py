"""仅供本地学习的 MCP 服务。stdio 每次启动有独立内存；HTTP 会话可共享内存。"""
import argparse
import json
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

parser=argparse.ArgumentParser();parser.add_argument('--http',action='store_true');parser.add_argument('--port',type=int,default=18021)
options=parser.parse_args()
mcp=FastMCP('AITC local demo',host='127.0.0.1',port=options.port,json_response=True)
notes=[]


def audit(name):
    if path:=os.environ.get('AITC_DEMO_AUDIT_FILE'):
        with Path(path).open('a') as out:out.write(name+'\n')


@mcp.tool(annotations={'readOnlyHint':True})
def lookup_rule(topic:str)->dict:
    """读取演示规则。topic 是查询主题；返回固定教学内容，不含真实业务数据。"""
    audit('lookup_rule')
    return {'topic':topic,'rule':'订单未支付时可以取消；已支付订单应先核实退款条件。','source':'local-demo','mock':True}


@mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False})
def record_demo_note(text:str)->dict:
    """写入此演示进程的内存便签，不影响实际项目。stdio 重连后便签清空。"""
    audit('record_demo_note');notes.append(text)
    return {'saved_in_demo_memory':True,'count':len(notes),'text':text,'mock':True}


if __name__=='__main__':mcp.run(transport='streamable-http' if options.http else 'stdio')
