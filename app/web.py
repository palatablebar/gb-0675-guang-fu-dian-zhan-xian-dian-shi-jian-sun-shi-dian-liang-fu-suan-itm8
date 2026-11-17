from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app.errors import BusinessError
from app import store

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def render(request:Request,key):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
        if key=='supplement' and 'supplement' not in state:
            if not state.get('input_snapshot'):
                raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
            # 旧批次未带工作台结果：按留存快照只读补齐，不改动原批记录。
            rebuilt=evaluate({'dataset':state['source']},snapshot=state['input_snapshot'])
            state={**rebuilt,'revision':state.get('revision',1),'status':state.get('status')}
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
    view=state['views'].get(key) or state.get(key) or {'title':key,'rows':[],'metrics':{}}
    return templates.TemplateResponse(request=request,name=f'{key}.html',context={'meta':META,'key':key,'state':state,'view':view})
