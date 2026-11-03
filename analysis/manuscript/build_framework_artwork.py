"""Build Figure 1 as editable PowerPoint and export a publication-size PDF.
Only reads geographic / frozen manuscript data. No experiment imports or calls.
Optional environment: GEOMETRY_PYTHON, RUNTIME_NODE, RUNTIME_NODE_MODULES,
RUNTIME_PYTHON, PRESENTATIONS_SKILL_DIR, SOFFICE, PDFTOPPM.
"""
from pathlib import Path
import os,subprocess,sys,json,hashlib,time
SOURCE=Path(__file__).resolve().parent
HERE=Path(os.environ.get('GRID_AGENT_MANUSCRIPT_ROOT','manuscript-workspace')).resolve()
def run(args):subprocess.run([str(a) for a in args],cwd=HERE,check=True,env={**os.environ,"GRID_AGENT_MANUSCRIPT_ROOT":str(HERE)})
def main():
    tmp=HERE/'work/framework_export';tmp.mkdir(parents=True,exist_ok=True)
    if '--export-only' not in sys.argv:
      run([os.environ.get('GEOMETRY_PYTHON',sys.executable),SOURCE/'prepare_framework_geometry.py'])
      run([os.environ.get('RUNTIME_NODE','node'),SOURCE/'build_framework_ppt.mjs'])
    soffice=os.environ.get('SOFFICE','soffice')
    profile=(tmp/('lo_profile_'+str(time.time_ns()))).as_uri()
    run([soffice,'-env:UserInstallation='+profile,'--headless','--convert-to','pdf','--outdir',tmp,HERE/'figures/fig1_framework.pptx'])
    code="from pypdf import PdfReader,PdfWriter;import sys;r=PdfReader(sys.argv[1]);w=PdfWriter();p=r.pages[0];p.scale_to(180/25.4*72,180/25.4*72*1060/1500);w.add_page(p);w.add_metadata({'/Title':'Figure 1: regional planning, simulation and physical acceptance','/Creator':'Editable PowerPoint; LibreOffice PDF export'});w.write(sys.argv[2])"
    run([os.environ.get('RUNTIME_PYTHON',sys.executable),'-c',code,tmp/'fig1_framework.pdf',HERE/'figures/fig1_framework.pdf'])
    run([os.environ.get('PDFTOPPM','pdftoppm'),'-scale-to','2250','-png','-singlefile',HERE/'figures/fig1_framework.pdf',HERE/'figures/fig1_framework'])
    run([soffice,'-env:UserInstallation='+profile,'--headless','--convert-to','svg:impress_svg_Export','--outdir',tmp,HERE/'figures/fig1_framework.pptx'])
    (HERE/'figures/fig1_framework.svg').write_bytes((tmp/'fig1_framework.svg').read_bytes())
    def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
    geo=json.loads((HERE/'evidence/integrated_loop_20261006/framework_geometry.json').read_text())
    info=dict(builder='build_framework_ppt.mjs',geometry_builder='prepare_framework_geometry.py',reported_cohort=96,regions=8,model_zones=geo['model_zones'],native_editable_pptx=True,slide_count=1,black_text=True,lettered_panels=False,rectangular_stage_boxes=False,colour_meanings={'orange':'solar capacity/profile','purple':'nuclear capacity','teal':'battery power','blue':'new wind capacity/profile/dispatch','black':'inherited assets/dispatch, text and forward data flow','red':'numerical evidence, critique and redesign'},spatial_semantics=geo['spatial_semantics'],profile_semantics=geo['profile_semantics'],dispatch_semantics=geo['dispatch_semantics'],dispatch_candidate_sha256=geo['dispatch_candidate_sha256'],width_mm=180,height_mm=180*1060/1500,outputs={ext:sha(HERE/('figures/fig1_framework.'+ext)) for ext in ['pptx','pdf','png','svg']},sources=geo['sources'],builder_sha256=sha(SOURCE/'build_framework_ppt.mjs'),geometry_builder_sha256=sha(SOURCE/'prepare_framework_geometry.py'),export_builder_sha256=sha(Path(__file__)),new_inference_calls=0,new_optimizer_calls=0,new_power_flow_calls=0)
    for p in ['figures/fig1_framework_audit.json','evidence/integrated_loop_20261006/artwork_specification.json']:(HERE/p).write_text(json.dumps(info,indent=2)+'\n')
    print(json.dumps({'exported':'Figure 1 PowerPoint / PDF / SVG / PNG','width_mm':180,'regions':len(geo['regions'])}))
if __name__=='__main__':main()
