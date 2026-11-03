"""Download CENACE 'Estimacion de la Demanda Real por Balance' daily CSVs (hourly demand by
Gerencia de Control Regional) for a year, replicating the public SIM page's ASP.NET postbacks.
Latest settlement (highest 'No. de Liquidacion') is kept per operating day."""
import urllib.request,urllib.parse,re,html,http.cookiejar,json,sys,time,datetime,pathlib
from tqdm import tqdm
URL='https://www.cenace.gob.mx/Paginas/SIM/Reportes/EstimacionDemandaReal.aspx'
import os
OUT=pathlib.Path(os.environ.get('GRID_DATA_ROOT', str(pathlib.Path(__file__).resolve().parents[3] / 'data'))).expanduser().resolve()/'experiment/mexico/raw/demanda_real'
def session():
    cj=http.cookiejar.CookieJar();op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders=[('User-Agent','Mozilla/5.0'),('Referer',URL)];return op
def hidden_fields(page):
    form={}
    for m in re.finditer(r'<input type="hidden" name="([^"]+)"(?: id="[^"]*")? value="([^"]*)"',page):form[m.group(1)]=html.unescape(m.group(2))
    return form
def post(op,form):
    req=urllib.request.Request(URL,data=urllib.parse.urlencode(form).encode());r=op.open(req,timeout=180);return r,r.read()
def datepick(form,prefix,d):
    y,m,dd=d.strftime('%Y'),d.strftime('%m'),d.strftime('%d')
    form[f'ctl00$ContentPlaceHolder1${prefix}']=f'{y}-{m}-{dd}'
    form[f'ctl00$ContentPlaceHolder1${prefix}$dateInput']=f'{dd}/{m}/{y}'
    form[f'ctl00_ContentPlaceHolder1_{prefix}_dateInput_ClientState']=json.dumps({"enabled":True,"emptyMessage":"","validationText":f"{y}-{m}-{dd}-00-00-00","valueAsString":f"{y}-{m}-{dd}-00-00-00","minDateStr":"1980-01-01-00-00-00","maxDateStr":"2099-12-31-00-00-00","lastSetTextBoxValue":f"{dd}/{m}/{y}"})
    form[f'ctl00_ContentPlaceHolder1_{prefix}_ClientState']=json.dumps({"minDateStr":"1980-01-01-00-00-00","maxDateStr":"2099-12-31-00-00-00"})
def fetch_day(op,page,d):
    form=hidden_fields(page);datepick(form,'RadDatePickerVisualizarPorBalance',d)
    form['__EVENTTARGET']='ctl00$ContentPlaceHolder1$RadDatePickerVisualizarPorBalance';form['__EVENTARGUMENT']=''
    r,body=post(op,form);page=body.decode('utf-8','ignore')
    buttons=re.findall(r'name="(ctl00\$ContentPlaceHolder1\$GridRadPorBalance\$ctl00\$ctl\d+\$gbccolumn)"',page)
    dates=re.findall(r'<td class="Letras">(\d\d/\d\d/\d{4}) 12:00:00',page)
    if not buttons or not dates or dates[0]!=d.strftime('%d/%m/%Y'):return page,None,f'no rows for {d}: {dates[:2]}'
    form=hidden_fields(page);form[buttons[-1]+'.x']='5';form[buttons[-1]+'.y']='5'
    r,body=post(op,form)
    if 'octet-stream' not in (r.headers.get('Content-Type') or ''):return page,None,'no csv returned'
    name=re.search(r'filename=(.*)',r.headers.get('Content-Disposition','')).group(1).strip()
    return page,(name,body),None
if __name__=='__main__':
    year=int(sys.argv[1]) if len(sys.argv)>1 else 2023
    OUT.mkdir(parents=True,exist_ok=True);op=session();page=op.open(URL,timeout=120).read().decode('utf-8','ignore')
    days=[datetime.date(year,1,1)+datetime.timedelta(days=i) for i in range(366 if year%4==0 else 365)]
    failures=[]
    for d in tqdm(days,desc='CENACE demand'):
        target=OUT/f'demanda_real_balance_{d.isoformat()}.csv'
        if target.exists():continue
        for attempt in range(3):
            try:
                page,res,err=fetch_day(op,page,d)
                if res:target.write_bytes(res[1]);(OUT/'filenames.log').open('a').write(f'{d.isoformat()}\t{res[0]}\n');break
                failures.append((d.isoformat(),err));break
            except Exception as e:
                time.sleep(5);op=session();page=op.open(URL,timeout=120).read().decode('utf-8','ignore')
                if attempt==2:failures.append((d.isoformat(),repr(e)))
    print('failures',failures)
