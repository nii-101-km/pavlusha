"""Fixed built-in ODT and ODS operations; no arbitrary function loader."""
from dataclasses import replace
import json
import base64
import shlex
from pathlib import Path
from pavlusha_agent.extensions import Contribution, Outcome
from pavlusha_modules.util import declare, TEXT, STRING, INTEGER


class Office:
    name='office'
    def __init__(self, root, output_limit, enable_calc=False, enable_basic_macro=False):
        self.root=root; self.output_limit=output_limit
        self.enable_calc=enable_calc; self.enable_basic_macro=enable_basic_macro; self.calc=None
    def on_event(self,event):
        if event.kind=='session_started':
            from .calc_adapter import LibreOfficeCalcAdapter
            self.calc=LibreOfficeCalcAdapter(self.root)
            tools=[]
            descriptions={
                'odt_inspect':'Inspect top-level Writer paragraphs and tables; returns revision and paragraph IDs. LibreOffice reads a copy.',
                'odt_read':'Read top-level paragraphs and table cells. Use current revision and optional node_ids or contains.',
                'odt_edit':'Replace exact text in top-level paragraphs by node_id, old and new. Save a new copy; LibreOffice conversion may change complex features.',
                'odt_render':'Render a ODT copy to a PDF and page PNGs in a new directory.',
                'ods_inspect':'Inspect ODS sheets and their used dimensions; returns revision.',
                'ods_read':'Read formulas and calculated values from an ODS cell range with LibreOffice Calc.',
                'ods_edit':'Edit values or formulas in a new recalculated ODS copy via LibreOffice. Conversion may change workbook features.',
                'ods_render':'Render an ODS copy to a PDF and page PNGs in a new directory.'}
            for kind in ('odt','ods'):
                for operation in ('inspect','read','edit','render'):
                    name=kind+'_'+operation; props,required=self.schema(kind,operation)
                    tools.append(replace(declare(name,descriptions[name],props,required,
                        lambda args,ctx,k=kind,op=operation:Outcome(self.native_call(k,op,args))), programmatic=True))
            tools.append(replace(declare('ods_create',
                'Create a new empty LibreOffice Calc ODS workbook with one sheet; default sheet name Sheet1. No overwrite.',
                {'output_path':TEXT,'sheet':TEXT},('output_path',),
                lambda args,ctx:Outcome(self.calc_call('create',args))),programmatic=True))
            paragraphs={'type':'array','minItems':1,'maxItems':64,'items':{
                'type':'object','properties':{'text':TEXT,'role':{'type':'string','enum':['title','body']},
                'align':{'type':'string','enum':['left','right','center','justify']}},
                'required':['text'],'additionalProperties':False}}
            tools.append(replace(declare('odt_create',
                'Create a new ODT using LibreOffice Writer. Supply paragraphs as {text, role: title|body, align?}. '
                'Default font Liberation Serif, 14 pt, justified body with 1.25 cm first-line indent, bold centered title, single line spacing and zero paragraph spacing. '
                'paragraph_after_pt sets spacing after each paragraph in points; choose 14 for a blank line at 14 pt. No overwrite.',
                {'output_path':TEXT,'paragraphs':paragraphs,'font':TEXT,
                 'size':{'type':'number','minimum':8,'maximum':72},
                 'alignment':{'type':'string','enum':['left','right','center','justify']},
                 'paragraph_after_pt':{'type':'number','minimum':0,'maximum':72},
                 'first_line_indent_cm':{'type':'number','minimum':0,'maximum':5}},
                ('output_path','paragraphs'),lambda args,ctx:Outcome(self.create_odt(**args))),programmatic=True))
            tools.append(replace(declare('odt_add_table',
                'Insert a simple table in a new copy of a ODT via LibreOffice Writer. '
                'Use the current revision from odt_inspect, 1..50 rows with equal-length arrays of 1..10 text cells, '
                'optional bold repeating first row (header=true), alignment left/center/right, caption, '
                'and after_text containing exact unique top-level paragraph text; omit after_text to append. '
                'No overwrite; LibreOffice conversion may alter complex document features.',
                {'path':TEXT,'output_path':TEXT,'revision':TEXT,
                 'rows':{'type':'array','minItems':1,'maxItems':50,'items':{'type':'array','minItems':1,'maxItems':10,'items':STRING}},
                 'header':{'type':'boolean'},'alignment':{'type':'string','enum':['left','center','right']},'caption':STRING,'after_text':TEXT},
                ('path','output_path','revision','rows'),lambda args,ctx:Outcome(self.add_table(**args))),programmatic=True))
            tools.append(replace(declare('odt_add_image',
                'Insert a PNG or JPEG from the workdir into a new ODT copy via LibreOffice Writer. '
                'Supply current revision from odt_inspect, image_path, width_cm (1..16), optional caption, '
                'alignment left/center/right and descriptive alt_text. after_text selects one unique exact top-level paragraph; omit to append. '
                'No overwrite; LibreOffice may alter complex document features.',
                {'path':TEXT,'output_path':TEXT,'revision':TEXT,'image_path':TEXT,
                 'width_cm':{'type':'number','minimum':1,'maximum':16},
                 'alignment':{'type':'string','enum':['left','center','right']},'caption':STRING,'alt_text':STRING,'after_text':TEXT},
                ('path','output_path','revision','image_path'),lambda args,ctx:Outcome(self.add_image(**args))),programmatic=True))
            rows_prop={'type':'array','minItems':1,'maxItems':50,'items':{'type':'array','minItems':1,'maxItems':10,'items':STRING}}
            tools.append(replace(declare('odt_replace_table',
                'Replace an entire top-level table in a new ODT copy. table_index is 1-based in document order; '
                'use odt_inspect to inspect the document. Supply rows as equal-length arrays of text cells. '
                'No overwrite; formatting and content inside the old table are replaced.',
                {'path':TEXT,'output_path':TEXT,'revision':TEXT,'table_index':{'type':'integer','minimum':1,'maximum':200},
                 'rows':rows_prop,'header':{'type':'boolean'},'alignment':{'type':'string','enum':['left','center','right']}},
                ('path','output_path','revision','table_index','rows'),lambda args,ctx:Outcome(self.replace_table(**args))),programmatic=True))
            tools.append(replace(declare('odt_insert_paragraph',
                'Insert a body paragraph or bold title after one unique, exact top-level paragraph (after_text), or append if omitted. '
                'Use odt_inspect and odt_read to obtain text and revision. alignment: left/right/center/justify. No overwrite.',
                {'path':TEXT,'output_path':TEXT,'revision':TEXT,'text':TEXT,'after_text':TEXT,
                 'role':{'type':'string','enum':['title','body']},'alignment':{'type':'string','enum':['left','right','center','justify']}},
                ('path','output_path','revision','text'),lambda args,ctx:Outcome(self.insert_paragraph(**args))),programmatic=True))
            tools.append(replace(declare('odt_insert_list',
                'Insert a one-level bullet or numbered list after a unique exact top-level paragraph (after_text), or append if omitted. '
                'Items are text. No overwrite.',
                {'path':TEXT,'output_path':TEXT,'revision':TEXT,'items':{'type':'array','minItems':1,'maxItems':50,'items':TEXT},
                 'kind':{'type':'string','enum':['bullet','numbered']},'after_text':TEXT,
                 'alignment':{'type':'string','enum':['left','right','center','justify']}},
                ('path','output_path','revision','items'),lambda args,ctx:Outcome(self.insert_list(**args))),programmatic=True))
            tools.append(replace(declare('odt_format_paragraph',
                'Change alignment or mark as title/body for one unique exact top-level paragraph in a new ODT copy. '
                'match_text is the full paragraph text; role title is bold and body is not. No overwrite.',
                {'path':TEXT,'output_path':TEXT,'revision':TEXT,'match_text':TEXT,
                 'alignment':{'type':'string','enum':['left','right','center','justify']},'role':{'type':'string','enum':['title','body']}},
                ('path','output_path','revision','match_text'),lambda args,ctx:Outcome(self.format_paragraph(**args))),programmatic=True))
            tools.append(replace(declare('odt_normalize',
                'Apply one font and size to body and ordinary table text in a new ODT copy. '
                'Uses single line spacing, zero paragraph spacing, and a 1.25 cm first-line indent only on ordinary body paragraphs. '
                'Only title/heading styles and table header rows remain bold; '
                'italics are removed. Optional alignment applies to body paragraphs, not headings or table cells. '
                'Headers, footers and complex objects are not normalized. No overwrite.',
                {'path':TEXT,'output_path':TEXT,'revision':TEXT,'font':TEXT,
                 'size':{'type':'number','minimum':8,'maximum':72},
                 'alignment':{'type':'string','enum':['left','right','center','justify']}},
                ('path','output_path','revision'),lambda args,ctx:Outcome(self.normalize_document(**args))),programmatic=True))
            if self.enable_calc:
                chart_props={'path':TEXT,'output_path':TEXT,'revision':TEXT,'sheet':TEXT,'cell_range':TEXT,
                    'anchor':TEXT,'name':TEXT,'type':{'type':'string','enum':['scatter','line','column','pie']},
                    'title':STRING,'x_title':STRING,'y_title':STRING,'legend':{'type':'boolean'},
                    'headers':{'type':'boolean'}}
                tools.append(replace(declare('ods_chart_create',
                    'Create a chart in a new ODS copy via LibreOffice. Data is a rectangular range on sheet: first column X/categories, '
                    'subsequent columns series; headers=true uses the first row as series names. type scatter/line/column/pie; '
                    'anchor is the top-left cell. Optional title, x_title, y_title are text; legend and headers are booleans. '
                    'For example: title="Growth", x_title="Time", y_title="Length", legend=true. '
                    'Pie requires exactly two columns. No overwrite; '
                    'LibreOffice may change workbook features. Chart names can change after saving; use ods_chart_list.',
                    chart_props,('path','output_path','revision','sheet','cell_range','anchor','name','type'),
                    lambda args,ctx:Outcome(self.calc_call('chart',args))),programmatic=True))
                tools.append(replace(declare('ods_chart_list',
                    'List charts on an ODS sheet with their 1-based indices for PNG export.',
                    {'path':TEXT,'revision':TEXT,'sheet':TEXT},('path','revision','sheet'),
                    lambda args,ctx:Outcome(self.calc_call('chart_list',args))),programmatic=True))
                tools.append(replace(declare('ods_chart_export',
                    'Save a chart from ODS as a new PNG under workdir, e.g. output_path plots/chart.png. '
                    'Parent directory must already exist. Use ods_chart_list for chart_index, then view_image to inspect PNG.',
                    {'path':TEXT,'revision':TEXT,'sheet':TEXT,'output_path':TEXT,'chart_index':{'type':'integer','minimum':1,'maximum':100}},
                    ('path','revision','sheet','output_path'),lambda args,ctx:Outcome(self.calc_call('chart_export',args))),programmatic=True))
                surface_props={'path':TEXT,'output_path':TEXT,'image_path':TEXT,'revision':TEXT,'sheet':TEXT,
                    'cell_range':TEXT,'anchor':TEXT,'title':STRING,'x_title':STRING,'y_title':STRING,'z_title':STRING}
                tools.append(replace(declare('calc_ods_inspect',
                    'Inspect an ODS workbook, return its SHA-256 revision and sheet names/used dimensions. '
                    'Optionally supply sheet and cell_range to read up to 100 cells with values and formulas; '
                    'without a range the preview is A1:J10. Opens a temporary copy with macros disabled.',
                    {'path':TEXT,'sheet':TEXT,'cell_range':TEXT},('path',),
                    lambda args,ctx:Outcome(self.inspect_ods(**args))),programmatic=True))
                tools.append(replace(declare('calc_surface_create',
                    'Create a static 3D surface picture from an ODS Calc sheet and insert it into a new ODS copy. '
                    'cell_range includes an empty top-left cell, strictly increasing numeric X values across the first row, '
                    'strictly increasing numeric Y values down the first column, and finite numeric Z values in the grid. '
                    'Example: A1:D4 has X in B1:D1, Y in A2:A4, Z in B2:D4. Use 2..50 values per axis. '
                    'Supply relative path, output_path (.ods), image_path (.png), SHA-256 revision, sheet, and anchor. '
                    'Outputs must not exist and parent folders must exist. The image is static: rerun after changing data. '
                    'Optional title, x_title, y_title, z_title control labels.',
                    surface_props,('path','output_path','image_path','revision','sheet','cell_range','anchor'),
                    lambda args,ctx:Outcome(self.create_surface(**args))),programmatic=True))
            if self.enable_basic_macro:
                tools.append(replace(declare('calc_basic_macro',
                    'Run LibreOffice Basic source in an isolated shell environment on a new blank Calc workbook and save it as ODS. '
                    'Code must define Sub Main and can use ThisComponent. The macro runs with access to the entire working directory. '
                    'For Calc Solver, Objective and Variables require CellAddress structs from cell.getCellAddress(), not cell objects; '
                    'constraints use SolverConstraint structs. Tool ok means the macro returned, not that an internally caught error or Solver succeeded; verify results. '
                    'output_path is a new simple filename ending .ods. Macros are embedded in the output. Runtime limit: 60 seconds.',
                    {'code':TEXT,'output_path':TEXT},('code','output_path'),
                    lambda args,ctx:Outcome(self.basic_macro(**args))),programmatic=True))
            return Contribution(message='Built-in ODT and ODS tools read and edit files within the work directory. '
                'Render requires LibreOffice/Poppler; inspect generated page images. '
                'LibreOffice conversions can modify complex document features.',tools=tuple(tools))
        if event.kind=='session_stopped':
            self.calc=None
    def native_call(self,kind,operation,args):
        from .calc_adapter import OfficeAdapterError
        from .writer_append import inspect_writer, replace_writer_text
        from .render_adapter import render
        def page(items,cursor,revision,query,limit):
            import hashlib
            token=hashlib.sha256(json.dumps([revision,query],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
            start=0
            if cursor is not None:
                if not isinstance(cursor,str) or not cursor.startswith(token+':') or not cursor[len(token)+1:].isdigit():
                    raise ValueError('Invalid or stale pagination cursor')
                start=int(cursor[len(token)+1:])
                if start>len(items): raise ValueError('Invalid pagination cursor')
            selected=items[start:start+limit]
            next_cursor=token+':'+str(start+len(selected)) if start+len(selected)<len(items) else None
            return selected,next_cursor
        try:
            args=dict(args)
            if operation=='render':
                result=render(self.root,**args,suffix='.'+kind)
            elif kind=='ods':
                if operation=='inspect':
                    sheet=args.pop('sheet',None);cursor=args.pop('cursor',None);limit=args.pop('limit',20)
                    if type(limit) is not int or not 1<=limit<=100: raise ValueError('limit must be 1..100')
                    result=self.calc.inspect(**args)
                    result['path']=args['path'];result['type']='workbook'
                    if sheet is not None:
                        result['sheets']=[s for s in result['sheets'] if s['name']==sheet]
                        if not result['sheets']: raise ValueError('Sheet not found')
                    result['sheets'],result['next_cursor']=page(result['sheets'],cursor,result['revision'],['ods',sheet],limit)
                elif operation=='read':
                    cursor=args.pop('cursor',None); max_chars=args.pop('max_chars',8000)
                    if type(max_chars) is not int or not 1800<=max_chars<=12000:
                        raise ValueError('max_chars must be 1800..12000')
                    result=self.calc.read(**args)
                    cells=result.pop('cells');result['revision']=result['source_revision']
                    query=['ods_read',args['sheet'],args['cell_range']]
                    limit=len(cells)
                    while True:
                        selected,next_cursor=page(cells,cursor,result['revision'],query,limit)
                        if len(json.dumps({'ok':True,**result,'cells':selected,'next_cursor':next_cursor},ensure_ascii=False))<=min(max_chars,self.output_limit): break
                        if limit<=1: raise ValueError('One cell exceeds output budget')
                        limit=max(1,limit//2)
                    result.update(cells=selected,next_cursor=next_cursor)
                else: result=self.calc.edit(**args)
            else:
                if operation=='edit': result=replace_writer_text(self.root,**args)
                else:
                    cursor=args.pop('cursor',None)
                    limit=args.pop('limit',40);max_chars=args.pop('max_chars',8000)
                    ids=args.pop('node_ids',None);contains=args.pop('contains',None)
                    result=inspect_writer(self.root,**args)
                    items=[]
                    for node in result.pop('nodes'):
                        item=dict(node)
                        if 'node_id' in item: item['id']=item['node_id']
                        items.append(item)
                    if ids is not None:
                        if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids): raise ValueError('Invalid node IDs')
                        items=[i for i in items if i.get('id') in ids]
                        if len(items)!=len(set(ids)): raise ValueError('Node ID not found')
                    if contains is not None:
                        if not isinstance(contains,str) or not contains: raise ValueError('Invalid search text')
                        items=[i for i in items if contains in i.get('text','')]
                    if type(limit) is not int or not 1<=limit<=100: raise ValueError('limit must be 1..100')
                    if type(max_chars) is not int or not 1800<=max_chars<=12000: raise ValueError('max_chars must be 1800..12000')
                    while True:
                        selected,next_cursor=page(items,cursor,result['revision'],['odt',operation,ids,contains],limit)
                        if len(json.dumps({**result,'items':selected,'next_cursor':next_cursor},ensure_ascii=False))<=min(max_chars,self.output_limit): break
                        if limit<=1: raise ValueError('One document item exceeds output budget')
                        limit=max(1,limit//2)
                    result.update(items=selected,next_cursor=next_cursor)
            result={'ok':True,**result}
            if len(json.dumps(result,ensure_ascii=False))>self.output_limit:
                raise ValueError('Result exceeds output limit; use a smaller selection')
            return result
        except FileNotFoundError as exc:
            return {'ok':False,'error':{'code':'file_not_found','message':str(exc)[:1000]}}
        except (ValueError,OSError,OfficeAdapterError) as exc:
            return {'ok':False,'error':{'code':'office_error','message':str(exc)[:1000]}}
    def calc_call(self, operation, args):
        from .calc_adapter import OfficeAdapterError
        try:
            result={'ok':True,**getattr(self.calc,operation)(**args)}
            if len(json.dumps(result,ensure_ascii=False)) > self.output_limit:
                return {'ok':False,'error':{'code':'result_limit_exceeded','message':'Use a smaller range.'}}
            return result
        except OfficeAdapterError as exc:
            return {'ok':False,'error':{'code':'calc_error','message':str(exc)[:1000]}}
    def create_surface(self, **args):
        from .surface_adapter import create_surface
        return create_surface(self.root,**args)
    def add_table(self, **args):
        from .writer_append import add_table
        return add_table(self.root,**args)
    def add_image(self, **args):
        from .writer_append import add_image
        return add_image(self.root,**args)
    def replace_table(self, **args):
        from .writer_append import replace_table
        return replace_table(self.root,**args)
    def insert_paragraph(self, **args):
        from .writer_append import insert_paragraph
        return insert_paragraph(self.root,**args)
    def insert_list(self, **args):
        from .writer_append import insert_list
        return insert_list(self.root,**args)
    def format_paragraph(self, **args):
        from .writer_append import format_paragraph
        return format_paragraph(self.root,**args)
    def normalize_document(self, **args):
        from .writer_append import normalize_document
        return normalize_document(self.root,**args)
    def inspect_ods(self, **args):
        from .surface_adapter import inspect_ods
        return inspect_ods(self.root,**args)
    def create_odt(self, output_path, paragraphs, font='Liberation Serif', size=14,
                    alignment='justify', paragraph_after_pt=0, first_line_indent_cm=1.25):
        from pavlusha_agent.sandbox import run_shell
        import math
        if (not isinstance(output_path,str) or len(output_path)>200 or Path(output_path).name != output_path
                or not output_path.lower().endswith('.odt') or not isinstance(font,str) or not 1<=len(font)<=100
                or type(size) not in (int,float) or not math.isfinite(size) or not 8<=size<=72
                or type(paragraph_after_pt) not in (int,float) or not math.isfinite(paragraph_after_pt)
                or not 0<=paragraph_after_pt<=72 or type(first_line_indent_cm) not in (int,float)
                or not math.isfinite(first_line_indent_cm) or not 0<=first_line_indent_cm<=5
                or alignment not in ('left','right','center','justify')
                or not isinstance(paragraphs,list) or not 1<=len(paragraphs)<=64):
            return {'ok':False,'error':{'code':'invalid_request','message':'Invalid document settings or output filename.'}}
        for p in paragraphs:
            if (not isinstance(p,dict) or set(p)-{'text','role','align'} or not isinstance(p.get('text'),str)
                    or not 1<=len(p['text'])<=8000 or any(c in p['text'] for c in '\r\n\x00')
                    or p.get('role','body') not in ('title','body')
                    or p.get('align',alignment) not in ('left','right','center','justify')):
                return {'ok':False,'error':{'code':'invalid_request','message':'Each paragraph must have single-line text and valid formatting.'}}
        if (Path(self.root)/output_path).exists() or (Path(self.root)/output_path).is_symlink():
            return {'ok':False,'error':{'code':'destination_exists','message':'Choose a new output filename.'}}
        source=Path(__file__).with_name('_writer_sandbox_worker.py').read_bytes()
        runner='import base64;exec(base64.b64decode('+repr(base64.b64encode(source).decode())+'))'
        request=json.dumps({'output':output_path,'paragraphs':paragraphs,'font':font,'size':size,
                            'alignment':alignment,'paragraph_after_pt':paragraph_after_pt,
                            'first_line_indent_cm':first_line_indent_cm},ensure_ascii=True)
        try:
            result=run_shell(Path(self.root),'/usr/bin/python3 -c '+shlex.quote(runner)+' '+shlex.quote(request),network=False,timeout=60)
            if result.timed_out:
                return {'ok':False,'error':{'code':'timeout','message':'Writer exceeded 60 seconds.'}}
            try: data=json.loads(result.stdout)
            except ValueError: data={'ok':False,'error':(result.stderr or 'Writer worker failed')[:1000]}
            if not data.get('ok') or result.exit_code != 0:
                return {'ok':False,'error':{'code':'writer_error','message':str(data.get('error','Writer failed'))[:1000]}}
            return data
        except OSError as exc:
            return {'ok':False,'error':{'code':'writer_error','message':str(exc)[:1000]}}
    def basic_macro(self, code, output_path):
        from pavlusha_agent.sandbox import run_shell
        if (not isinstance(code,str) or not 1 <= len(code) <= 16000 or '\x00' in code
                or not isinstance(output_path,str) or len(output_path)>200
                or Path(output_path).name != output_path or output_path in ('.','..')
                or not output_path.lower().endswith('.ods')):
            return {'ok':False,'error':{'code':'invalid_request','message':'Provide Basic code and a new simple .ods filename.'}}
        if (Path(self.root)/output_path).exists() or (Path(self.root)/output_path).is_symlink():
            return {'ok':False,'error':{'code':'destination_exists','message':'Choose a new output filename.'}}
        source=Path(__file__).with_name('_macro_sandbox_worker.py').read_bytes()
        runner='import base64;exec(base64.b64decode('+repr(base64.b64encode(source).decode())+'))'
        request=json.dumps({'code':code,'output':output_path},ensure_ascii=True)
        command='/usr/bin/python3 -c '+shlex.quote(runner)+' '+shlex.quote(request)
        try:
            result=run_shell(Path(self.root),command,network=False,timeout=60)
            if result.timed_out:
                return {'ok':False,'error':{'code':'timeout','message':'Macro exceeded 60 seconds.'}}
            try:
                data=json.loads(result.stdout)
            except ValueError:
                data={'ok':False,'error':(result.stderr or 'Macro worker failed')[:1000]}
            if not data.get('ok') or result.exit_code != 0:
                return {'ok':False,'error':{'code':'macro_error','message':str(data.get('error','Macro failed'))[:1000]}}
            return data
        except OSError as exc:
            return {'ok':False,'error':{'code':'macro_error','message':str(exc)[:1000]}}
    @staticmethod
    def schema(kind, operation):
        if operation=='inspect':
            props={'path':TEXT,'cursor':STRING,'limit':INTEGER}
            if kind=='ods': props['sheet']=STRING
            return props,('path',)
        if operation=='read':
            props={'path':TEXT,'revision':TEXT,'cursor':STRING,'max_chars':INTEGER}
            if kind=='ods': props.update(sheet=TEXT,cell_range=TEXT); req=('path','revision','sheet','cell_range')
            else: props.update(node_ids={'type':'array','items':TEXT},contains=STRING); req=('path','revision')
            return props,req
        if operation=='edit':
            return {'path':TEXT,'output_path':TEXT,'revision':TEXT,
                    'edits':{'type':'array','items':{'type':'object'}}},('path','output_path','revision','edits')
        return {'path':TEXT,'revision':TEXT,'output_dir':TEXT,'dpi':INTEGER,'timeout_seconds':INTEGER},('path','revision','output_dir')


def create_extension(*, settings, context):
    if set(settings)-{'enable_calc','enable_basic_macro'} or any(type(settings.get(k,False)) is not bool for k in ('enable_calc','enable_basic_macro')):
        raise ValueError('Office accepts boolean enable_calc and enable_basic_macro')
    return Office(context['workdir'],context.get('output_limit',24000),settings.get('enable_calc',False),settings.get('enable_basic_macro',False))
