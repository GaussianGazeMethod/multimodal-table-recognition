# -*- coding: utf-8 -*-
"""文档解析（大模型版）单页隔离验收。"""
import json,os,time,sys,urllib.parse
from pathlib import Path
from dotenv import load_dotenv
from alibabacloud_docmind_api20220711.client import Client
from alibabacloud_docmind_api20220711 import models
from alibabacloud_tea_openapi import models as openmodels
from alibabacloud_tea_util import models as utilmodels
root=Path(__file__).resolve().parents[2];source=Path(sys.argv[1]).resolve();out=Path(sys.argv[2]) if len(sys.argv)>2 else Path(__file__).resolve().parent/('api-'+source.stem);out.mkdir(exist_ok=False);load_dotenv(root/'.env');start=time.monotonic()
def sanitize(value):
 if isinstance(value,dict):return {k:sanitize(v) for k,v in value.items()}
 if isinstance(value,list):return [sanitize(v) for v in value]
 if isinstance(value,str) and value.startswith(('http://','https://')) and '?' in value:
  u=urllib.parse.urlsplit(value);return urllib.parse.urlunsplit((u.scheme,u.netloc,u.path,'',''))+'?[签名已移除]'
 return value
def save(name,data):(out/name).write_text(json.dumps(sanitize(data),ensure_ascii=False,indent=2),encoding='utf-8')
try:
 c=Client(openmodels.Config(access_key_id=os.environ['ALIBABA_CLOUD_ACCESS_KEY_ID'],access_key_secret=os.environ['ALIBABA_CLOUD_ACCESS_KEY_SECRET'],endpoint='docmind-api.cn-hangzhou.aliyuncs.com'))
 with source.open('rb') as f:
  request=models.SubmitDocParserJobAdvanceRequest(file_name=source.name,file_url_object=f,output_format=['visualLayoutInfo'],need_header_footer=True)
  submitted=c.submit_doc_parser_job_advance(request,utilmodels.RuntimeOptions()).body.to_map()
 save('submit.json',submitted)
 if not (submitted.get('Data') or {}).get('Id'):raise RuntimeError('提交被拒绝: '+str(submitted.get('Code'))+' '+str(submitted.get('Message')))
 task=submitted['Data']['Id'];print('任务提交成功，完整输入文件',flush=True)
 for i in range(36):
  time.sleep(5);x=c.query_doc_parser_status(models.QueryDocParserStatusRequest(id=task)).body.to_map();save(f'status-{i:02}.json',x);data=x.get('Data') or {};status=str(data.get('Status',x.get('Status',''))).lower();print('查询',i,status,round(time.monotonic()-start,1),flush=True)
  if status in ('success','failed','fail'):break
 else:raise TimeoutError('查询超时；任务 ID 已保存')
 if status!='success':raise RuntimeError('任务未成功: '+status)
 parts=[]
 for offset in range(0,3000,300):
  z=c.get_doc_parser_result(models.GetDocParserResultRequest(id=task,layout_num=offset,layout_step_size=300)).body.to_map();save(f'result-{offset}.json',z);layouts=(z.get('Data') or {}).get('layouts',[]);parts.extend(layouts)
  if len(layouts)<300:break
 save('summary.json',{'source':str(source),'page_index':'all','seconds':round(time.monotonic()-start,3),'layout_count':len(parts),'token':'未知','api':'SubmitDocParserJobAdvance/QueryDocParserStatus/GetDocParserResult'});print('完成',len(parts),'秒',round(time.monotonic()-start,2),flush=True)
except Exception as e:
 msg=str(e)
 for name in ('ALIBABA_CLOUD_ACCESS_KEY_ID','ALIBABA_CLOUD_ACCESS_KEY_SECRET'):
  if os.getenv(name):msg=msg.replace(os.environ[name],'[已隐藏]')
 save('error.json',{'type':type(e).__name__,'message':msg,'seconds':round(time.monotonic()-start,3)});print('失败',type(e).__name__,msg[:300],flush=True);sys.exit(1)
