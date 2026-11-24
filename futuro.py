#!/usr/bin/env python3
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import yfinance as yf

NY=ZoneInfo("America/New_York")
TICKERS=["TSLA","NVDA","AAPL","SPY","QQQ","IWM","DIA"]
FUTURES={"SPY":"ES=F","QQQ":"NQ=F","IWM":"RTY=F","DIA":"YM=F"}
GREEN="\033[92m"; RED="\033[91m"; WHITE="\033[97m"; CYAN="\033[96m"; RESET="\033[0m"; BOLD="\033[1m"

def dl(s):
 d=yf.download(s,period="5d",interval="1m",prepost=True,auto_adjust=False,repair=True,progress=False,threads=False,multi_level_index=False)
 if d.empty: raise RuntimeError("sin datos")
 d=d.dropna(subset=["Close"])
 if d.index.tz is None:d.index=d.index.tz_localize("UTC")
 d.index=d.index.tz_convert(NY);return d

def pct(a,b):return (a/b-1)*100 if b and np.isfinite(b) else np.nan
def ago(d,n):
 x=d[d.index<=d.index[-1]-pd.Timedelta(minutes=n)];return float(x.Close.iloc[-1]) if len(x) else np.nan
def prevclose(d,day):
 r=d.between_time("09:30","16:00",inclusive="left");r=r[r.index.date<day]
 if r.empty:return np.nan
 q=r.index[-1].date();return float(r[r.index.date==q].Close.iloc[-1])
def session(ts):
 t=ts.time()
 if dtime(4)<=t<dtime(9,30):return "PRE"
 if dtime(9,30)<=t<dtime(16):return "REGULAR"
 if dtime(16)<=t<=dtime(20):return "POST"
 return "CERRADO"
def analyze(s):
 d=dl(s);ts=d.index[-1].to_pydatetime();price=float(d.Close.iloc[-1]);pc=prevclose(d,ts.date())
 return {"ticker":s,"session":session(ts),"price":price,"gap":pct(price,pc),"m5":pct(price,ago(d,5)),"h1":pct(price,ago(d,60)),"time":ts.strftime("%H:%M")}
def score(r,ctx=None):
 z=50+np.clip(r['h1']*12,-22,22)+np.clip(r['m5']*35,-18,18)
 if r['gap']<=-.40 and r['h1']>0:z+=5
 elif r['gap']>=.40 and r['h1']<0:z-=5
 if ctx:z+=np.clip(ctx['h1']*5,-8,8)+np.clip(ctx['m5']*10,-5,5)
 return float(np.clip(z,5,95))
def label(p):
 if p>=60:return GREEN,"UP"
 if p<=40:return RED,"DOWN"
 return WHITE,"LATERAL"
def main():
 while True:
  fc={}
  for f in set(FUTURES.values()):
   try:fc[f]=analyze(f)
   except:fc[f]=None
  print("\n"+"="*91);print(f"{BOLD}OPENING BIAS | {datetime.now(NY):%Y-%m-%d %H:%M:%S ET}{RESET}")
  print(f"{'Ticker':<7}{'Price':>10}{'Gap%':>9}{'5m%':>9}{'60m%':>9}  {'Bias':<12}{'ScoreUp':>8} {'ET':>6}")
  for s in TICKERS:
   try:
    r=analyze(s);p=score(r,fc.get(FUTURES.get(s)));c,b=label(p)
    line=f"{s:<7}{r['price']:>10.2f}{r['gap']:>+9.2f}{r['m5']:>+9.2f}{r['h1']:>+9.2f}  {b:<12}{p:>7.0f}% {r['time']:>6}"
    print(c+line+RESET)
   except Exception as e:print(RED+f"{s:<7} ERROR: {e}"+RESET)
  print(f"\n{GREEN}GREEN >=60% UP{RESET} | {WHITE}WHITE 41-59% LATERAL{RESET} | {RED}RED <=40% DOWN{RESET}")
  print("ScoreUp is a heuristic directional score, not a backtested probability.")
  time.sleep(60)
if __name__=="__main__":main()
