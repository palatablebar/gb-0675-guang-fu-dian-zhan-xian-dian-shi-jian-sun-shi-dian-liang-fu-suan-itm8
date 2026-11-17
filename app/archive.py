import io, json, zipfile
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        evidence={'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']}
        if 'supplement' in result:
            report=result['supplement']
            evidence['supplement']=report
            evidence['voided']=result.get('voided',[])
        if 'balance' in result: evidence['balance']=result['balance']
        archive.writestr('evidence.json',json.dumps(evidence,ensure_ascii=False,indent=2))
    return buffer.getvalue()
