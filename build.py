#!/usr/bin/env python3
"""Regenerates index.html from data/final_100.jsonl. Usage: python build.py [data.jsonl] [out.html]"""
import json,sys,html
src=sys.argv[1] if len(sys.argv)>1 else 'final_100.jsonl'
out=sys.argv[2] if len(sys.argv)>2 else 'index.html'
keep=['id','app','category','vendor_url','auth_methods_value','auth_methods_tier','auth_methods_evidence_url','auth_methods_quote','auth_methods_evidence_kind',
'access_model_value','access_coarse_value','access_model_tier','access_model_evidence_url','access_model_quote','access_model_evidence_kind','access_model_note',
'official_mcp_value','official_mcp_tier','official_mcp_note','official_mcp_evidence_url','official_mcp_quote','official_mcp_evidence_kind',
'api_type_value','api_type_tier','api_type_evidence_url','api_type_quote','api_type_evidence_kind','buildability','primary_blocker','buildability_tier','composio_catalog_matched','composio_slug']
desc=json.load(open('descriptions.json',encoding='utf-8'))
run=open('run_cmds.txt',encoding='utf-8').read().strip() if __import__('os').path.exists('run_cmds.txt') else 'See the README in the repo for the run steps.'
rows=[dict({k:r.get(k) for k in keep},desc=desc.get(str(r['id']),'')) for r in map(json.loads,open(src,encoding='utf-8'))]
data=json.dumps(rows,ensure_ascii=False).replace('</','<\\/')
tpl=open('template.html',encoding='utf-8').read()
open(out,'w',encoding='utf-8').write(tpl.replace('/*DATA*/[]',data).replace('RUN_CMDS',html.escape(run)))
print('wrote',out,len(rows),'rows')
