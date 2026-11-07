"""Render a numeric ODS grid as a static surface and insert it into Calc."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.connection import NoConnectException
from PIL import Image, ImageDraw, ImageFont


def prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def plot(xs, ys, zs, path, title, labels):
    image = Image.new('RGB', (1200, 900), 'white')
    draw = ImageDraw.Draw(image)
    regular = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    bold = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    font = ImageFont.truetype(regular, 24)
    small = ImageFont.truetype(regular, 19)
    heading = ImageFont.truetype(bold, 29)
    draw.text((600, 30), title, font=heading, fill='#202c3a', anchor='ma')
    zmin = min(min(row) for row in zs)
    zmax = max(max(row) for row in zs)
    zspan = zmax - zmin or 1
    def point(x, y, z):
        px = (x-xs[0])/(xs[-1]-xs[0])
        py = (y-ys[0])/(ys[-1]-ys[0])
        pz = (z-zmin)/zspan
        return (int(580 + (px-py)*340), int(560+(px+py)*80-pz*400))
    faces = []
    for j in range(len(ys)-1):
        for i in range(len(xs)-1):
            points = [point(xs[i],ys[j],zs[j][i]), point(xs[i+1],ys[j],zs[j][i+1]),
                      point(xs[i+1],ys[j+1],zs[j+1][i+1]),point(xs[i],ys[j+1],zs[j+1][i])]
            avg = (zs[j][i]+zs[j][i+1]+zs[j+1][i+1]+zs[j+1][i])/4
            faces.append((i/(len(xs)-1)+j/(len(ys)-1),points,(avg-zmin)/zspan))
    for _, points, f in sorted(faces, key=lambda row: row[0]):
        fill = (int(34+210*f), int(105+80*f), int(186-120*f))
        draw.polygon(points, fill=fill, outline='#ffffff', width=1)
    origin=point(xs[0],ys[-1],zmin); xp=point(xs[-1],ys[-1],zmin); yp=point(xs[0],ys[0],zmin); zp=point(xs[0],ys[-1],zmax)
    for end in (xp,yp,zp): draw.line((origin,end), fill='#263645',width=3)
    def label(text, pos, width=520):
        size=24
        current=font
        while size>10 and draw.textlength(text,font=current)>width:
            size-=1
            current=ImageFont.truetype(regular,size)
        draw.text(pos,text,font=current,fill='#263645')
    label(labels[0],(xp[0]+10,xp[1]+8),width=1200-xp[0]-20)
    label(labels[1],(yp[0]+10,yp[1]+12),width=1200-yp[0]-20)
    draw.text((origin[0]-10,origin[1]+12),f'{xs[0]:g}, {ys[-1]:g}',font=small,fill='#263645',anchor='ra')
    label(labels[2],(zp[0]+12,zp[1]-35),width=800)
    if not any(c.isdigit() for c in labels[0]):
        draw.text((xp[0]+10,xp[1]+38),f'{xs[-1]:g}',font=small,fill='#263645')
    if not any(c.isdigit() for c in labels[1]):
        draw.text((yp[0]+10,yp[1]+42),f'{ys[0]:g}',font=small,fill='#263645')
    draw.text((zp[0]-10,zp[1]-10),f'{zmax:g}',font=small,fill='#263645',anchor='ra')
    draw.text((origin[0]-10,origin[1]-10),f'{zmin:g}',font=small,fill='#263645',anchor='ra')
    for h in range(240):
        f=1-h/239
        draw.line((1100,180+h,1120,180+h),fill=(int(34+210*f),int(105+80*f),int(186-120*f)),width=1)
    draw.text((1090,150),f'{zmax:g}',font=small,fill='#263645',anchor='ra')
    draw.text((1090,425),f'{zmin:g}',font=small,fill='#263645',anchor='ra')
    image.save(path, format='PNG')


def main(request):
    pipe='pavlusha_'+uuid.uuid4().hex
    profile=Path('/tmp/profile-'+uuid.uuid4().hex);profile.mkdir()
    proc=subprocess.Popen(['/usr/bin/soffice','-env:UserInstallation='+profile.as_uri(),
        '--headless','--norestore','--nodefault','--nofirststartwizard',
        '--accept=pipe,name='+pipe+';urp;StarOffice.ComponentContext'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    doc=None
    try:
        local=uno.getComponentContext()
        resolver=local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver',local)
        for _ in range(150):
            try:
                ctx=resolver.resolve('uno:pipe,name='+pipe+';urp;StarOffice.ComponentContext');break
            except NoConnectException:
                if proc.poll() is not None: raise RuntimeError('LibreOffice failed to start')
                time.sleep(.1)
        else: raise RuntimeError('LibreOffice startup timed out')
        desktop=ctx.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop',ctx)
        doc=desktop.loadComponentFromURL(Path('/work',request['snapshot']).as_uri(),'_blank',0,
            (prop('Hidden',True),prop('MacroExecutionMode',0),prop('UpdateDocMode',0)))
        if doc is None: raise RuntimeError('Could not open ODS workbook')
        doc.calculateAll()
        if request.get('action') == 'inspect':
            sheets=[]
            for name in doc.Sheets.getElementNames():
                ws=doc.Sheets.getByName(name)
                cursor=ws.createCursor();cursor.gotoEndOfUsedArea(True);area=cursor.getRangeAddress()
                sheets.append({'name':name,'used_rows':area.EndRow+1,'used_columns':area.EndColumn+1})
            result={'ok':True,'sheets':sheets}
            if request.get('sheet'):
                ws=doc.Sheets.getByName(request['sheet'])
                area=ws.getCellRangeByName(request.get('cell_range','A1:J10')).getRangeAddress()
                width=area.EndColumn-area.StartColumn+1;height=area.EndRow-area.StartRow+1
                if width*height>100 or width<=0 or height<=0: raise RuntimeError('Read at most 100 cells')
                cells=[]
                for row in range(area.StartRow,area.EndRow+1):
                    for col in range(area.StartColumn,area.EndColumn+1):
                        cell=ws.getCellByPosition(col,row);kind=cell.Type.value
                        cells.append({'row':row+1,'column':col+1,'type':kind,'display':cell.String[:500],
                                      'value':cell.Value if kind in ('VALUE','FORMULA') else (cell.String[:500] if kind=='TEXT' else None),
                                      'formula':cell.Formula[:500] if kind=='FORMULA' else None,'error':cell.Error})
                result['cells']=cells
            return result
        sheet=doc.Sheets.getByName(request['sheet'])
        address=sheet.getCellRangeByName(request['cell_range']).getRangeAddress()
        cols=address.EndColumn-address.StartColumn+1; rows=address.EndRow-address.StartRow+1
        if not 3<=cols<=51 or not 3<=rows<=51: raise RuntimeError('Grid must contain 2..50 X and Y values plus headers')
        def value(col,row):
            cell=sheet.getCellByPosition(address.StartColumn+col,address.StartRow+row)
            if cell.Type.value not in ('VALUE','FORMULA') or cell.Error or not math.isfinite(cell.Value):
                raise RuntimeError('All X, Y and Z values must be finite numbers')
            return cell.Value
        xs=[value(i,0) for i in range(1,cols)]
        ys=[value(0,j) for j in range(1,rows)]
        zs=[[value(i,j) for i in range(1,cols)] for j in range(1,rows)]
        if any(a>=b for a,b in zip(xs,xs[1:])) or any(a>=b for a,b in zip(ys,ys[1:])):
            raise RuntimeError('X and Y headers must be strictly increasing')
        image_path=Path('/work',request['temp_image'])
        plot(xs,ys,zs,image_path,request['title'],(request['x_title'],request['y_title'],request['z_title']))
        anchor=sheet.getCellRangeByName(request['anchor']).getCellAddress()
        if anchor.Column>255 or anchor.Row>999: raise RuntimeError('Anchor must be within A1:IV1000')
        pos=uno.createUnoStruct('com.sun.star.awt.Point')
        pos.X=sum(sheet.Columns.getByIndex(i).Width for i in range(anchor.Column))
        pos.Y=sum(sheet.Rows.getByIndex(i).Height for i in range(anchor.Row))
        size=uno.createUnoStruct('com.sun.star.awt.Size');size.Width=16000;size.Height=12000
        shape=doc.createInstance('com.sun.star.drawing.GraphicObjectShape')
        sheet.getDrawPage().add(shape);shape.setPosition(pos);shape.setSize(size)
        shape.GraphicURL=image_path.as_uri()
        doc.storeAsURL(Path('/work',request['temp_document']).as_uri(),(prop('FilterName','calc8'),prop('Overwrite',False)))
        return {'ok':True,'rows':len(ys),'columns':len(xs),'z_min':min(map(min,zs)),'z_max':max(map(max,zs)),
                'rendering':'static image; rebuild after data changes'}
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
