import json, urllib.request
def C(c,p=None,t=120):
    r=urllib.request.Request('http://127.0.0.1:19723'); r.add_header('Content-Type','application/json')
    return json.loads(urllib.request.urlopen(r,json.dumps({'command':c,'parameters':p or {}}).encode(),timeout=t).read())
def T(c,p=None,t=120):
    res=C('API.ExecuteAddOnCommand',{'addOnCommandId':{'commandNamespace':'TapirCommand','commandName':c},'addOnCommandParameters':p or {}},t)
    if not res.get('succeeded'): raise RuntimeError(json.dumps(res)[:400])
    return res['result']['addOnCommandResponse']
