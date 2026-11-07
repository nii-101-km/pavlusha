"""Bounded Writer modifications on a sandboxed ODT copy."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.connection import NoConnectException
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
from PIL import Image


def prop(name,value):
    p=PropertyValue();p.Name=name;p.Value=value
    return p


def top_level(text):
    it=text.createEnumeration()
    while it.hasMoreElements():
        yield it.nextElement()


def paragraph(text, expected):
    matches=[p for p in top_level(text) if p.supportsService('com.sun.star.text.Paragraph') and p.String==expected]
    if len(matches)!=1:
        raise RuntimeError('Expected exactly one top-level paragraph with that text; found '+str(len(matches)))
    return matches[0]


def cursor_after(text, after_text):
    if after_text is None:
        cursor=text.createTextCursor();cursor.gotoEnd(False)
    else:
        node=paragraph(text,after_text)
        cursor=text.createTextCursorByRange(node.getEnd())
        cursor.gotoEndOfParagraph(False)
    text.insertControlCharacter(cursor,PARAGRAPH_BREAK,False)
    return cursor


def body_style(cursor,font,size,alignment='left',bold=False):
    cursor.ParaStyleName='Standard'
    cursor.CharFontName=font;cursor.CharHeight=size
    cursor.CharWeight=150.0 if bold else 100.0
    cursor.CharPosture=uno.Enum('com.sun.star.awt.FontSlant','NONE')
    cursor.ParaAdjust={'left':0,'right':1,'justify':2,'center':3}[alignment]
    cursor.ParaFirstLineIndent=0;cursor.ParaLeftMargin=0;cursor.ParaRightMargin=0
    cursor.ParaTopMargin=0;cursor.ParaBottomMargin=0
    spacing=uno.createUnoStruct('com.sun.star.style.LineSpacing');spacing.Mode=0;spacing.Height=100
    cursor.ParaLineSpacing=spacing


def main(request):
    pipe='pavlusha_'+uuid.uuid4().hex
    profile=Path('/tmp/profile-'+uuid.uuid4().hex);profile.mkdir()
    proc=subprocess.Popen(['/usr/bin/soffice','-env:UserInstallation='+profile.as_uri(),
        '--headless','--norestore','--nodefault','--nofirststartwizard',
        '--accept=pipe,name='+pipe+';urp;StarOffice.ComponentContext'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    doc=None
    try:
        ctx0=uno.getComponentContext()
        resolver=ctx0.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver',ctx0)
        for _ in range(150):
            try: ctx=resolver.resolve('uno:pipe,name='+pipe+';urp;StarOffice.ComponentContext');break
            except NoConnectException:
                if proc.poll() is not None: raise RuntimeError('LibreOffice failed to start')
                time.sleep(.1)
        else: raise RuntimeError('LibreOffice startup timed out')
        desktop=ctx.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop',ctx)
        doc=desktop.loadComponentFromURL(Path('/work',request['snapshot']).as_uri(),'_blank',0,
            (prop('Hidden',True),prop('MacroExecutionMode',0),prop('UpdateDocMode',0)))
        if doc is None: raise RuntimeError('Could not open ODT')
        text=doc.Text;start=text.createTextCursor();start.gotoStart(False)
        font=start.CharFontName or 'Liberation Serif';size=start.CharHeight or 14.0
        action=request['action']; after=request.get('after_text')
        if action == 'inspect':
            nodes=[]; table_index=0; paragraph_index=0
            for node in top_level(text):
                if node.supportsService('com.sun.star.text.Paragraph'):
                    paragraph_index+=1
                    nodes.append({'type':'paragraph','node_id':'paragraph:'+str(paragraph_index),
                                  'text':node.String,'style':node.ParaStyleName})
                elif node.supportsService('com.sun.star.text.TextTable'):
                    table_index+=1
                    cells=[]
                    for cell_name in node.CellNames:
                        cells.append({'cell':cell_name,'text':node.getCellByName(cell_name).String})
                    nodes.append({'type':'table','node_id':'table:'+str(table_index),'table_index':table_index,'name':node.Name,
                                  'rows':node.Rows.Count,'columns':node.Columns.Count,'cells':cells})
            return {'ok':True,'nodes':nodes}
        if action == 'replace_text':
            paragraphs=[node for node in top_level(text)
                        if node.supportsService('com.sun.star.text.Paragraph')]
            for edit in request['edits']:
                index=edit['index']
                if not 0 <= index < len(paragraphs): raise RuntimeError('Paragraph not found')
                node=paragraphs[index]
                if node.String.count(edit['old']) != 1: raise RuntimeError('Old text must occur exactly once')
                search=doc.createSearchDescriptor();search.SearchString=edit['old'];search.SearchCaseSensitive=True
                found=doc.findFirst(search)
                matches=[]
                while found:
                    if text.compareRegionStarts(found,node.getStart()) <= 0 and text.compareRegionEnds(found,node.getEnd()) >= 0:
                        matches.append(found)
                    found=doc.findNext(found.End,search)
                if len(matches)!=1: raise RuntimeError('Cannot select exact paragraph text')
                matches[0].String=edit['new']
            result={'edited':len(request['edits'])}
        elif action=='normalize':
            applied=0; indented=0
            def normalize_para(container,node,heading=False,alignment=None,first_line_indent=0):
                nonlocal applied
                c=container.createTextCursorByRange(node.getStart());c.gotoEndOfParagraph(True)
                c.CharFontName=request['font'];c.CharFontNameComplex=request['font'];c.CharFontNameAsian=request['font']
                c.CharHeight=float(request['size']);c.CharHeightComplex=float(request['size']);c.CharHeightAsian=float(request['size'])
                c.CharWeight=150.0 if heading else 100.0
                c.CharPosture=uno.Enum('com.sun.star.awt.FontSlant','NONE')
                c.ParaFirstLineIndent=first_line_indent;c.ParaLeftMargin=0;c.ParaRightMargin=0;c.ParaTopMargin=0;c.ParaBottomMargin=0
                spacing=uno.createUnoStruct('com.sun.star.style.LineSpacing');spacing.Mode=0;spacing.Height=100
                c.ParaLineSpacing=spacing
                if alignment is not None: c.ParaAdjust={'left':0,'right':1,'justify':2,'center':3}[alignment]
                applied+=1
            for node in top_level(text):
                if node.supportsService('com.sun.star.text.Paragraph'):
                    name=node.ParaStyleName or ''
                    heading=name=='Title' or name.startswith('Heading')
                    is_list=bool(node.NumberingStyleName) or bool(node.ListId)
                    is_caption=name in ('Caption','Illustration','Table')
                    indent=1250 if node.String and not (heading or is_list or is_caption) else 0
                    if indent: indented+=1
                    normalize_para(text,node,heading,None if heading else request['alignment'],indent)
                elif node.supportsService('com.sun.star.text.TextTable'):
                    for ri in range(node.Rows.Count):
                        for ci in range(node.Columns.Count):
                            ct=node.getCellByPosition(ci,ri).getText()
                            for sub in top_level(ct):
                                if sub.supportsService('com.sun.star.text.Paragraph'):
                                    normalize_para(ct,sub,bool(node.RepeatHeadline and ri<node.HeaderRowCount))
            result={'updated':'body_and_table_text','paragraphs':applied,'font':request['font'],'size_pt':request['size'],
                    'first_line_indent_cm':1.25,'indented_body_paragraphs':indented,
                    'unindented':'titles, lists, captions, empty paragraphs and table cells'}
        elif action=='format':
            node=paragraph(text,request['match_text']);cursor=text.createTextCursorByRange(node.getStart());cursor.gotoEndOfParagraph(True)
            if request.get('alignment') is not None:
                cursor.ParaAdjust={'left':0,'right':1,'justify':2,'center':3}[request['alignment']]
            if request.get('role') is not None:
                cursor.ParaStyleName='Title' if request['role']=='title' else 'Standard'
                cursor.CharFontName=font;cursor.CharHeight=size
                cursor.CharWeight=150.0 if request['role']=='title' else 100.0
                cursor.CharPosture=uno.Enum('com.sun.star.awt.FontSlant','NONE')
                cursor.ParaFirstLineIndent=0 if request['role']=='title' else 1250
                cursor.ParaLeftMargin=0;cursor.ParaRightMargin=0
                cursor.ParaTopMargin=0;cursor.ParaBottomMargin=0
                spacing=uno.createUnoStruct('com.sun.star.style.LineSpacing');spacing.Mode=0;spacing.Height=100
                cursor.ParaLineSpacing=spacing
            result={'updated':'paragraph'}
        elif action in ('table','replace_table'):
            if action=='replace_table':
                tables=[n for n in top_level(text) if n.supportsService('com.sun.star.text.TextTable')]
                index=request['table_index']
                if index>len(tables): raise RuntimeError('Table index not found')
                table=tables[index-1]
            else:
                cursor=cursor_after(text,after)
                if request.get('caption'):
                    body_style(cursor,font,size)
                    cursor.ParaStyleName='Caption';cursor.CharFontName=font;cursor.CharHeight=size;cursor.CharWeight=100.0
                    cursor.CharPosture=uno.Enum('com.sun.star.awt.FontSlant','NONE');cursor.ParaFirstLineIndent=0
                    text.insertString(cursor,request['caption'],False)
                    text.insertControlCharacter(cursor,PARAGRAPH_BREAK,False)
            rows=request['rows']
            if action=='replace_table':
                old_rows=table.Rows.Count;old_columns=table.Columns.Count
                if len(rows)>old_rows: table.Rows.insertByIndex(old_rows,len(rows)-old_rows)
                elif len(rows)<old_rows: table.Rows.removeByIndex(len(rows),old_rows-len(rows))
                if len(rows[0])>old_columns: table.Columns.insertByIndex(old_columns,len(rows[0])-old_columns)
                elif len(rows[0])<old_columns: table.Columns.removeByIndex(len(rows[0]),old_columns-len(rows[0]))
            else:
                table=doc.createInstance('com.sun.star.text.TextTable')
                table.initialize(len(rows),len(rows[0]));text.insertTextContent(cursor,table,False)
            table.RepeatHeadline=request['header']
            if request['header']:
                table.RepeatHeadline=True;table.HeaderRowCount=1
            borders=uno.createUnoStruct('com.sun.star.table.TableBorder')
            line=uno.createUnoStruct('com.sun.star.table.BorderLine');line.Color=0xD9D9D9;line.OuterLineWidth=25
            for attr in ('TopLine','BottomLine','LeftLine','RightLine','HorizontalLine','VerticalLine'):
                setattr(borders,attr,line);setattr(borders,'Is'+attr+'Valid',True)
            table.TableBorder=borders
            for ri,row in enumerate(rows):
                for ci,value in enumerate(row):
                    cell=table.getCellByPosition(ci,ri);cell.String=value
                    content=cell.getText();c=content.createTextCursor();c.gotoStart(False);c.gotoEnd(True)
                    body_style(c,font,size,request['alignment'],request['header'] and ri==0)
            result={'rows':len(rows),'columns':len(rows[0]),'header':request['header']}
        elif action=='image':
            cursor=cursor_after(text,after)
            src=Path('/work',request['image_snapshot'])
            with Image.open(src) as image:
                width,height=image.size
                if width<=0 or height<=0 or width*height>25_000_000: raise RuntimeError('Image has invalid or excessive dimensions')
                image.verify()
            width_mm=round(request['width_cm']*1000);height_mm=round(width_mm*height/width)
            if height_mm>24000: raise RuntimeError('Image too tall for the page; choose smaller width')
            graphic=doc.createInstance('com.sun.star.text.TextGraphicObject')
            graphic.GraphicURL=src.as_uri();graphic.AnchorType=uno.Enum('com.sun.star.text.TextContentAnchorType','AS_CHARACTER')
            graphic.Width=width_mm;graphic.Height=height_mm
            if request['alt_text']: graphic.Description=request['alt_text']
            body_style(cursor,font,size,request['alignment'])
            text.insertTextContent(cursor,graphic,False)
            if request['caption']:
                cursor=text.createTextCursorByRange(graphic.getAnchor())
                if not cursor.goRight(1,False): raise RuntimeError('Could not position caption after image')
                text.insertControlCharacter(cursor,PARAGRAPH_BREAK,False)
                body_style(cursor,font,size,'center')
                cursor.ParaStyleName='Caption';cursor.CharFontName=font;cursor.CharHeight=size;cursor.CharWeight=100.0
                cursor.CharPosture=uno.Enum('com.sun.star.awt.FontSlant','NONE');cursor.ParaFirstLineIndent=0;cursor.ParaAdjust=3
                text.insertString(cursor,request['caption'],False)
            result={'width_cm':request['width_cm'],'height_cm':height_mm/1000,'alt_text':request['alt_text']}
        elif action=='paragraph':
            cursor=cursor_after(text,after)
            body_style(cursor,font,size,request['alignment'],request['role']=='title')
            if request['role']=='body': cursor.ParaFirstLineIndent=1250
            if request['role']=='title':
                cursor.ParaStyleName='Heading 1';cursor.CharFontName=font;cursor.CharHeight=size;cursor.CharWeight=150.0
            text.insertString(cursor,request['text'],False)
            result={'inserted':'paragraph'}
        elif action=='list':
            cursor=cursor_after(text,after);body_style(cursor,font,size,request['alignment'])
            rules=doc.createInstance('com.sun.star.text.NumberingRules')
            props=list(rules.getByIndex(0));new=[]
            for p in props:
                if p.Name=='NumberingType': p.Value=uno.Any('short',6 if request['kind']=='bullet' else 4)
                if p.Name=='BulletChar' and request['kind']=='bullet': p.Value='•'
                new.append(p)
            uno.invoke(rules,'replaceByIndex',(0,uno.Any('[]com.sun.star.beans.PropertyValue',tuple(new))))
            for i,item in enumerate(request['items']):
                if i: text.insertControlCharacter(cursor,PARAGRAPH_BREAK,False)
                body_style(cursor,font,size,request['alignment']);cursor.NumberingRules=rules;cursor.NumberingLevel=0
                text.insertString(cursor,item,False)
            result={'inserted':'list','kind':request['kind'],'items':len(request['items'])}
        else: raise RuntimeError('Unknown Writer operation')
        doc.storeAsURL(Path('/work',request['temp_output']).as_uri(),
            (prop('FilterName','writer8'),prop('Overwrite',False)))
        result['ok']=True
        return result
    finally:
        if doc is not None:
            try: doc.close(True)
            except Exception: pass
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill();proc.wait()


if __name__=='__main__':
    try: print(json.dumps(main(json.loads(sys.argv[1])),ensure_ascii=False,allow_nan=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));sys.exit(1)
