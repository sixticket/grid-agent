"""Local Responses transport with explicit history; no server-side state required."""
import copy,hashlib,json,os,pathlib,time
import openai
from openai._models import construct_type
from providers import OpenAIChat

def append_json(path,value):
 from exploration import context
 if context(): value=dict(value,exploration=context())
 with path.open('a') as f:
  f.write(json.dumps(value)+'\n');f.flush();os.fsync(f.fileno())

class LocalChat(OpenAIChat):
 output_dir=None
 def __init__(self,model):
  self.model=model;self.prev_id=None;self.pending=[];self.history=[]
  self.client=openai.OpenAI(base_url=os.environ.get('GRID_LOCAL_BASE_URL','http://127.0.0.1:1234/v1'),api_key='local-only',timeout=7200,max_retries=0)
  original=self.client.responses.create
  if os.environ.get('GRID_LOCAL_BACKEND')in ('llama-chat','vllm-chat'):
   from llama_responses import create_response
   original=lambda **kw: create_response(self.client,**kw)
  def create(**kw):
   # Logical previous IDs remain in logs for exact replay; wire input is self-contained.
   logical_prev=kw.pop('previous_response_id',None)
   requested_limit=kw.get('max_output_tokens')
   if os.environ.get('GRID_LOCAL_OUTPUT_TOKENS'):
    kw['max_output_tokens']=int(os.environ['GRID_LOCAL_OUTPUT_TOKENS'])
   kw['input']=copy.deepcopy(self.history)+kw['input'];kw['truncation']='disabled'
   out=self.output_dir;cache=out/'local_response_cache';cache.mkdir(exist_ok=True)
   digest=hashlib.sha256(json.dumps(kw,sort_keys=True).encode()).hexdigest();f=cache/(digest+'.json')
   t=time.time();append_json(out/'local_requests.jsonl',dict(time=t,request_sha256=digest,original_max_output_tokens=requested_limit,request=kw))
   saved=json.loads(f.read_text()) if f.exists() else None
   committed=set()
   log=out/'log.jsonl'
   if log.exists():
    for line in log.read_text().splitlines():
     e=json.loads(line);v=e.get('payload',{})
     if e['event']=='llm':committed.add(v['response'].get('id'))
     elif e['event']=='assistant':committed.add(v.get('id'))
   if saved is not None and saved.get('id') not in committed:
    r=construct_type(type_=openai.types.responses.Response, value=saved)
   else:
    try:
     r=original(**kw)
     append_json(out/'local_timing.jsonl',dict(start=t,seconds=time.time()-t,id=r.id,usage=r.usage.model_dump(),status=r.status))
     if r.status!='completed':
      append_json(out/'local_incomplete_responses.jsonl',dict(time=time.time(),request_sha256=digest,response=r.model_dump()))
      raise RuntimeError('Local response not completed: '+str(r.incomplete_details))
     raw=r.model_dump();tmp=f.with_suffix('.tmp')
     with tmp.open('w') as h:json.dump(raw,h);h.flush();os.fsync(h.fileno())
     tmp.replace(f)
    except BaseException as e:
     append_json(out/'local_errors.jsonl',dict(time=time.time(),seconds=time.time()-t,request_sha256=digest,error=str(e),error_type=type(e).__name__));raise
   r=r.model_copy(update={'previous_response_id':logical_prev,'instructions':kw['instructions']})
   self.restore_response(r.model_dump())
   return r
  self.client.responses.create=create
 def restore_response(self,raw):
  self.history.extend(copy.deepcopy(self.pending))
  # Preserve reasoning and tool items as well as prose; no history summarization.
  self.history.extend(copy.deepcopy(raw['output']))
