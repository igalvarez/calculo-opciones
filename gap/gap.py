#!/usr/bin/env python3
"""gap_analyzer.py v1.0 - Analizador de gaps para NVDA, TSLA y AAPL.
Uso: python gap_analyzer.py TICKER [--folder CARPETA] [--verbose]
"""
import argparse, sys, math
from pathlib import Path
import pandas as pd
import numpy as np

SUPPORTED = {"NVDA", "TSLA", "AAPL"}
AAPL_Q2_LOW, AAPL_Q2_HIGH = -2.38894, 0.43371

HIST = {
 "NVDA_CALL_BB_1":(43,60), "NVDA_CALL_BB_2":(34,45), "NVDA_CALL_BB_3":(17,19), "NVDA_CALL_ABB_ALT":(11,13),
 "TSLA_CALL_AB_1":(37,60), "TSLA_CALL_AB_2":(18,26), "TSLA_CALL_AB_3":(11,13),
 "TSLA_CALL_REC_1":(15,21), "TSLA_CALL_REC_2":(11,13), "TSLA_CALL_REC_3":(9,10),
 "TSLA_PUT_ABB_1":(11,14), "TSLA_PUT_ABB_2":(7,8),
 "AAPL_CALL_2B_1":(9,11), "AAPL_CALL_AAB_1":(9,12), "AAPL_CALL_AAB_2":(8,11),
 "AAPL_PUT_Q2_1":(18,25), "AAPL_PUT_Q2_2":(13,15)
}

def args():
 p=argparse.ArgumentParser(); p.add_argument("ticker", choices=sorted(SUPPORTED)); p.add_argument("--folder",default="."); p.add_argument("--verbose",action="store_true"); return p.parse_args()

def locate(folder,ticker,tf):
 folder=Path(folder); names = ([f"{ticker}-1h.txt",f"{ticker}-h.txt"] if tf=='H' else
  [f"{ticker}-1d.txt",f"{ticker}-d.txt"] if tf=='D' else [f"{ticker}-1w.txt",f"{ticker}-w.txt"])
 files={x.name.lower():x for x in folder.glob("*.txt")}
 for n in names:
  if n.lower() in files:return files[n.lower()]
 raise FileNotFoundError(f"No se encontró {tf}: {', '.join(names)}")

def parse_worden(path):
 """Reconstruye CSV de Worden con coma como separador y decimal."""
 lines=Path(path).read_text(encoding='utf-8-sig').splitlines(); parsed=[]
 for ln,line in enumerate(lines[1:],2):
  if not line.strip(): continue
  dt,rest=line.split(',',1); toks=rest.split(','); body=toks[:-2] if len(toks)>=2 and ('E' in toks[-1].upper()) else toks[:-1]
  states=[([],0)]
  for _ in range(9):
   ns=[]
   for vals,k in states:
    if k>=len(body):continue
    if body[k]=='':ns.append((vals+[np.nan],k+1))
    else:
     for n in (1,2):
      if k+n<=len(body) and all(body[k+j] for j in range(n)):
       try: ns.append((vals+[float(body[k]+('.'+body[k+1] if n==2 else ''))],k+n))
       except ValueError: pass
   states=ns
  candidates=[]
  for vals,k in states:
   if k==len(body):
    o,h,l,c,*z=vals
    if 0<l<=min(o,c)+1e-8 and h+1e-8>=max(o,c) and h/l<3.0 and all(pd.isna(x) or .05*c<x<20*c for x in z[:4]): candidates.append(vals)
  if not candidates: raise ValueError(f"No se pudo reconstruir {path.name}, línea {ln}")
  parsed.append((pd.to_datetime(dt,dayfirst=True),candidates))
 beam=[(0.,[v]) for v in parsed[0][1]][:100]
 for _,cs in parsed[1:]:
  nxt=[]
  for v in cs:
   opts=[]
   for cost,pathv in beam:
    pv=pathv[-1]; smooth=sum(abs(a-b) for a,b in zip(v[4:8],pv[4:8]) if pd.notna(a) and pd.notna(b))
    opts.append((cost+.03*abs(v[0]-pv[3])+2*smooth,pathv+[v]))
   nxt.append(min(opts,key=lambda x:x[0]))
  beam=sorted(nxt,key=lambda x:x[0])[:100]
 vals=min(beam,key=lambda x:x[0])[1]
 cols=['Date','Open','High','Low','Close','MA20','MA40','MA100','MA200','Worden']
 return pd.DataFrame([[parsed[i][0],*vals[i]] for i in range(len(vals))],columns=cols).sort_values('Date')

def validate(df,name):
 if df.Date.duplicated().any(): raise ValueError(f"Duplicados en {name}")
 if ((df.Low>df[['Open','Close']].min(axis=1)) | (df.High<df[['Open','Close']].max(axis=1))).any(): raise ValueError(f"OHLC inválido en {name}")

def sessions(H):
 H=H.copy(); H['day']=H.Date.dt.normalize()
 buy=np.maximum(H.Close-H.Open,0)+(np.minimum(H.Open,H.Close)-H.Low)
 sell=np.maximum(H.Open-H.Close,0)+(H.High-np.maximum(H.Open,H.Close))
 H['AB']=np.where(buy>sell,'A','B'); H['bal']=buy-sell
 out=[]
 for d,g in H.groupby('day',sort=True):
  r={'date':d,'Open':g.iloc[0].Open,'High':g.High.max(),'Low':g.Low.min(),'Close':g.iloc[-1].Close,
     'bars':len(g),'seq':''.join(g.AB),'B_count':int((g.AB=='B').sum()),'balance_sum':float(g.bal.sum())}
  for m in (20,40,100,200):r[f'H_MA{m}']=g.iloc[-1][f'MA{m}']
  out.append(r)
 return pd.DataFrame(out).sort_values('date').reset_index(drop=True)

def add_rule(out,id,family,level,side,label,experimental=False):
 w,n=HIST[id]; out.append(dict(id=id,family=family,level=level,side=side,label=label,wins=w,cases=n,rate=w/n,experimental=experimental))

def evaluate(ticker,x):
 r=[]; seq=x['seq']; hc=x['Close']; dc=x['D_Close']
 if ticker=='NVDA':
  if seq.endswith('BB'):
   add_rule(r,'NVDA_CALL_BB_1','NVDA_BB_CALL',1,'CALL','BB')
   if dc>x['D_MA100']:
    add_rule(r,'NVDA_CALL_BB_2','NVDA_BB_CALL',2,'CALL','BB + Close D > MA100D')
    if x['prev_gap_valid'] is True:add_rule(r,'NVDA_CALL_BB_3','NVDA_BB_CALL',3,'CALL','BB + D>MA100 + gap previo válido')
  if seq.endswith('ABB') and x['D_MA20']>x['D_MA40'] and hc>x['H_MA40']:
   add_rule(r,'NVDA_CALL_ABB_ALT','NVDA_ABB_CALL',1,'CALL','ABB + MA20D>MA40D + CloseH>MA40H')
 elif ticker=='TSLA':
  if seq.endswith('AB'):
   add_rule(r,'TSLA_CALL_AB_1','TSLA_AB_CALL',1,'CALL','AB')
   if dc>x['D_MA40']:
    add_rule(r,'TSLA_CALL_AB_2','TSLA_AB_CALL',2,'CALL','AB + CloseD>MA40D')
    if x['prev_gap_direction']=='DOWN':add_rule(r,'TSLA_CALL_AB_3','TSLA_AB_CALL',3,'CALL','AB + D>MA40 + gap previo bajista')
   if hc>x['H_MA20'] and dc<x['D_MA100']:
    add_rule(r,'TSLA_CALL_REC_1','TSLA_RECOVERY_CALL',1,'CALL','AB + H>MA20 + D<MA100')
    if x['prev_gap_valid'] is False:add_rule(r,'TSLA_CALL_REC_2','TSLA_RECOVERY_CALL',2,'CALL','AB + H>MA20 + D<MA100 + gap previo no válido')
    dist=100*(hc/x['H_MA20']-1)
    if 1<dist<=2:add_rule(r,'TSLA_CALL_REC_3','TSLA_RECOVERY_CALL',3,'CALL','AB + 1%-2% sobre MA20H + D<MA100')
  if seq.endswith('ABB') and hc>x['H_MA100']:
   add_rule(r,'TSLA_PUT_ABB_1','TSLA_ABB_PUT',1,'PUT','ABB + CloseH>MA100H')
   if 100*(hc/x['H_MA100']-1)>2:add_rule(r,'TSLA_PUT_ABB_2','TSLA_ABB_PUT',2,'PUT','ABB + >2% sobre MA100H',True)
 else:
  if x['B_count']==2 and hc<x['H_MA20']:add_rule(r,'AAPL_CALL_2B_1','AAPL_2B_CALL',1,'CALL','Exactamente 2 B + CloseH<MA20H')
  if seq.endswith('AAB'):
   if x['H_MA20']>x['H_MA40']:add_rule(r,'AAPL_CALL_AAB_1','AAPL_AAB_CALL',1,'CALL','AAB + MA20H>MA40H')
   if hc>x['H_MA200']:add_rule(r,'AAPL_CALL_AAB_2','AAPL_AAB_CALL_ALT',1,'CALL','AAB + CloseH>MA200H')
  q2=AAPL_Q2_LOW<x['balance_sum']<=AAPL_Q2_HIGH
  if q2 and hc>x['H_MA100']:
   add_rule(r,'AAPL_PUT_Q2_1','AAPL_Q2_PUT',1,'PUT','Balance Q2 + CloseH>MA100H')
   if 100*(hc/x['H_MA100']-1)>2:add_rule(r,'AAPL_PUT_Q2_2','AAPL_Q2_PUT',2,'PUT','Balance Q2 + >2% sobre MA100H')
 # highest level per family
 best={}
 for z in r:
  if z['family'] not in best or z['level']>best[z['family']]['level']:best[z['family']]=z
 return list(best.values())

def main():
 a=args(); folder=Path(a.folder); ticker=a.ticker.upper()
 try:
  paths={tf:locate(folder,ticker,tf) for tf in 'HDW'}
  H,D,W=(parse_worden(paths[tf]) for tf in 'HDW')
  for df,n in [(H,'H'),(D,'D'),(W,'W')]:validate(df,n)
  S=sessions(H)
  if len(S)<2:raise ValueError('Se necesitan al menos dos sesiones H')
  # Usa la sesión completa más reciente; ignora de forma explícita una sesión parcial al final.
  complete=S[S.bars==7]
  if len(complete)<2: raise ValueError('Se necesitan al menos dos sesiones H completas de 7 velas')
  cur=complete.iloc[-1].to_dict()
  prior=S[S.date < cur['date']]
  if prior.empty: raise ValueError('No existe sesión previa para calcular el gap')
  prev=prior.iloc[-1].to_dict()
  ddate=pd.Timestamp(cur['date']); drow=D[D.Date.dt.normalize()<=ddate].iloc[-1]
  if drow.Date.normalize()!=ddate:raise ValueError('No existe vela D alineada con la última sesión H')
  cur.update({'D_Close':drow.Close,**{f'D_MA{m}':drow[f'MA{m}'] for m in (20,40,100,200)}})
  # Previous gap belongs to current session: current open versus prior close; valid if session never crosses prior close adversely.
  gap=cur['Open']-prev['Close']; cur['prev_gap_direction']='UP' if gap>0 else 'DOWN' if gap<0 else 'FLAT'
  cur['prev_gap_valid']=bool(cur['Low']>=prev['Close']) if gap>0 else bool(cur['High']<=prev['Close']) if gap<0 else False
  needed=[cur.get(k) for k in ['H_MA20','H_MA40','H_MA100','H_MA200','D_MA20','D_MA40','D_MA100','D_MA200']]
  if any(pd.isna(v) for v in needed):raise ValueError('Falta alguna media requerida')
  rules=evaluate(ticker,cur); sides={r['side'] for r in rules}
  state='CONFLICTO' if len(sides)>1 else next(iter(sides)) if sides else 'NEUTRAL'
  print('='*64); print(f'TICKER: {ticker} | FECHA: {ddate.date()} | RESULTADO: {state}'); print('='*64)
  print(f"Secuencia H: {cur['seq']} | B: {cur['B_count']} | Balance: {cur['balance_sum']:.5f}")
  print(f"Gap previo: {cur['prev_gap_direction']} ({gap:.4f}) | Válido: {'Sí' if cur['prev_gap_valid'] else 'No'}")
  if rules:
   print('\nReglas activadas (máximo por familia):')
   for z in sorted(rules,key=lambda q:(q['side'],q['family'])):
    flag=' [EXPERIMENTAL N<10]' if z['experimental'] else ''
    print(f"- {z['side']} | {z['label']} | {z['wins']}/{z['cases']} = {100*z['rate']:.1f}%{flag}")
  else:print('\nSin ventaja histórica detectada.')
  if a.verbose:
   print('\nContexto:');
   for k in ['Close','H_MA20','H_MA40','H_MA100','H_MA200','D_Close','D_MA20','D_MA40','D_MA100','D_MA200']:print(f'  {k}: {cur[k]:.4f}')
  print('\nAviso: tasas históricas, no garantía prospectiva ni recomendación financiera.')
  return 0
 except Exception as e:
  print(f'NO EVALUABLE: {e}',file=sys.stderr); return 2
if __name__=='__main__':sys.exit(main())
