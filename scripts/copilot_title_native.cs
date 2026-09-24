// Read only a calibrated header patch or one fixed, bounded header probe.
// No focus, input, injection, full-window capture or disk I/O.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

public static class CopilotHeaderNative {
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left,Top,Right,Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct Point { public int X,Y; }
    public sealed class Target { public IntPtr Hwnd; public Rect Bounds; public uint Dpi,Pid; public long Started; public string Key; public int AssistantPid; }
    delegate bool EnumProc(IntPtr hwnd, IntPtr extra);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback,IntPtr extra);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd,uint command);
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr hwnd,uint flags);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd,out Rect rect);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd,StringBuilder name,int count);
    [DllImport("user32.dll",EntryPoint="GetWindowLongPtrW")] static extern IntPtr GetWindowLongPtr(IntPtr hwnd,int field);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(Point point);
    [DllImport("user32.dll")] static extern IntPtr GetWindowDC(IntPtr hwnd);
    [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr hwnd,IntPtr dc);
    [DllImport("gdi32.dll")] static extern bool BitBlt(IntPtr dst,int x,int y,int w,int h,IntPtr src,int sx,int sy,uint operation);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd,int attr,out int value,int size);

    public static bool ValidRoi(double x,double y,double width,double height) {
        foreach(double value in new[]{x,y,width,height})if(Double.IsNaN(value)||Double.IsInfinity(value))return false;
        return x>=80 && x<=1800 && y>=4 && width>=40 && width<=480 && height>=10 && height<=48 && y+height<=130;
    }
    public static RectangleF CalibrationProbe(Target target) {
        if(target==null||target.Dpi<96||target.Dpi>288)throw new ArgumentException("TITLE_AUTO_CALIBRATION_LAYOUT_UNSUPPORTED");
        double width=(target.Bounds.Right-target.Bounds.Left)/(target.Dpi/96.0);
        double height=(target.Bounds.Bottom-target.Bounds.Top)/(target.Dpi/96.0);
        if(width<700||width>2000||height<320)throw new ArgumentException("TITLE_AUTO_CALIBRATION_LAYOUT_UNSUPPORTED");
        // This profile is restricted to the already-checked 4.1.13 layout. Never
        // extend downward or fall back to the message body when OCR is absent.
        return new RectangleF(320,16,(float)Math.Min(width-400,1200),48);
    }
    public static RectangleF CalibrationRegion(Target target,RectangleF textBounds) {
        var probe=CalibrationProbe(target);double scale=target.Dpi/96.0;
        double left=textBounds.Left/scale,top=textBounds.Top/scale;
        double right=textBounds.Right/scale,bottom=textBounds.Bottom/scale;
        foreach(double value in new[]{left,top,right,bottom})
            if(Double.IsNaN(value)||Double.IsInfinity(value))throw new ArgumentException("TITLE_AUTO_CALIBRATION_UNREADABLE");
        if(textBounds.Width<=0||textBounds.Height<=0||left<6||top<4||bottom>probe.Height-4||
           right>probe.Width-8||left>probe.Width/2)throw new ArgumentException("TITLE_AUTO_CALIBRATION_UNREADABLE");
        // Keep horizontal room for a subsequent longer title; do not persist a
        // short current name's tight glyph rectangle as the ongoing read ROI.
        float x=(float)Math.Floor(probe.X+left-6);
        float width=Math.Min(480,probe.Right-8-x);
        if(probe.X+right>x+width-8||x<probe.X||!ValidRoi(x,probe.Y,width,probe.Height))
            throw new ArgumentException("TITLE_AUTO_CALIBRATION_UNREADABLE");
        return new RectangleF(x,probe.Y,width,probe.Height);
    }
    public static bool SameTarget(Target a,Target b) {
        return a!=null&&b!=null&&a.Hwnd==b.Hwnd&&a.Pid==b.Pid&&a.Started==b.Started&&a.Dpi==b.Dpi&&
            String.Equals(a.Key,b.Key,StringComparison.Ordinal)&&a.Bounds.Left==b.Bounds.Left&&
            a.Bounds.Top==b.Bounds.Top&&a.Bounds.Right==b.Bounds.Right&&a.Bounds.Bottom==b.Bounds.Bottom;
    }
    public static bool BlankCalibrationPadding(Bitmap bitmap,RectangleF textBounds,uint dpi) {
        if(bitmap==null||dpi<96||dpi>288||textBounds.Width<=0||textBounds.Height<=0)return false;
        double scale=dpi/96.0;
        int left=(int)Math.Floor(textBounds.Left-6*scale),top=0;
        int right=(int)Math.Ceiling(textBounds.Right+8*scale),bottom=bitmap.Height;
        if(left<0||top<0||right>bitmap.Width||bottom>bitmap.Height)return false;
        Color background=bitmap.GetPixel(left,top);
        for(int y=top;y<bottom;y++)for(int x=left;x<right;x++){
            // WinRT's vertical glyph bounds are approximate. Verify the actual
            // saved fixed-height strip's top/bottom, not a tight OCR-box crop.
            if(x>=Math.Floor(textBounds.Left)&&x<Math.Ceiling(textBounds.Right)&&
               y>=Math.Ceiling(4*scale)&&y<bitmap.Height-Math.Ceiling(4*scale))continue;
            Color pixel=bitmap.GetPixel(x,y);
            if(Math.Abs(pixel.R-background.R)>24||Math.Abs(pixel.G-background.G)>24||Math.Abs(pixel.B-background.B)>24)return false;
        }
        return true;
    }
    public static Target Find(int assistantPid) {
        SetThreadDpiAwarenessContext(new IntPtr(-4));
        var candidates=new List<Target>();
        EnumProc visit=delegate(IntPtr hwnd,IntPtr extra) {
            try {
                if(GetWindow(hwnd,4)!=IntPtr.Zero || GetAncestor(hwnd,2)!=hwnd)return true;
                long style=GetWindowLongPtr(hwnd,-16).ToInt64(), extended=GetWindowLongPtr(hwnd,-20).ToInt64();
                if((style&0x40000000)!=0 || (style&0x40000)==0 || (extended&0x08000081)!=0)return true;
                var cls=new StringBuilder(128);GetClassName(hwnd,cls,128);
                if(cls.ToString()!="Qt51514QWindowIcon" && cls.ToString()!="mmui::MainWindow")return true;
                uint pid;GetWindowThreadProcessId(hwnd,out pid);
                using(var process=Process.GetProcessById((int)pid)){
                    if(!String.Equals(process.ProcessName,"Weixin",StringComparison.OrdinalIgnoreCase))return true;
                    Rect rect;if(!GetWindowRect(hwnd,out rect))return true;
                    // Count hidden/minimized main windows too; do not guess between accounts.
                    if(!IsIconic(hwnd) && (rect.Right-rect.Left<480||rect.Bottom-rect.Top<320))return true;
                    string version=process.MainModule.FileVersionInfo.FileVersion;
                    if(version==null||!version.StartsWith("4.1.13.",StringComparison.Ordinal))throw new InvalidOperationException();
                    uint dpi=GetDpiForWindow(hwnd);if(dpi<96||dpi>288)return true;
                    string key;
                    using(var hash=SHA256.Create())key=BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(hwnd.ToInt64()+":"+pid+":"+process.StartTime.ToUniversalTime().Ticks))).Replace("-","").ToLowerInvariant();
                    candidates.Add(new Target{Hwnd=hwnd,Bounds=rect,Dpi=dpi,Pid=pid,Started=process.StartTime.ToUniversalTime().Ticks,Key=key,AssistantPid=assistantPid});
                }
            } catch { candidates.Add(null); }
            return true;
        };
        EnumWindows(visit,IntPtr.Zero);GC.KeepAlive(visit);
        if(candidates.Count!=1||candidates[0]==null)return null;
        var target=candidates[0];int cloaked;
        if(!IsWindowVisible(target.Hwnd)||IsIconic(target.Hwnd)||DwmGetWindowAttribute(target.Hwnd,14,out cloaked,4)!=0||cloaked!=0)return null;
        IntPtr active=GetForegroundWindow();uint activePid;GetWindowThreadProcessId(active,out activePid);
        uint targetPid;GetWindowThreadProcessId(target.Hwnd,out targetPid);
        if((activePid!=targetPid || GetAncestor(active,2)!=target.Hwnd) && activePid!=(uint)assistantPid)return null;
        return target;
    }
    static bool StillCurrent(Target target){
        try {
            uint pid;GetWindowThreadProcessId(target.Hwnd,out pid);Rect rect;
            if(pid!=target.Pid || !GetWindowRect(target.Hwnd,out rect) || !IsWindowVisible(target.Hwnd) || IsIconic(target.Hwnd))return false;
            if(rect.Left!=target.Bounds.Left||rect.Top!=target.Bounds.Top||rect.Right!=target.Bounds.Right||rect.Bottom!=target.Bounds.Bottom||GetDpiForWindow(target.Hwnd)!=target.Dpi)return false;
            int cloaked;
            if(DwmGetWindowAttribute(target.Hwnd,14,out cloaked,4)!=0||cloaked!=0)return false;
            IntPtr active=GetForegroundWindow();uint activePid;GetWindowThreadProcessId(active,out activePid);
            if((activePid!=target.Pid||GetAncestor(active,2)!=target.Hwnd)&&activePid!=(uint)target.AssistantPid)return false;
            using(var process=Process.GetProcessById((int)pid)){
                string version=process.MainModule.FileVersionInfo.FileVersion;
                return process.StartTime.ToUniversalTime().Ticks==target.Started&&version!=null&&version.StartsWith("4.1.13.",StringComparison.Ordinal);
            }
        }catch{return false;}
    }
    public static Bitmap Capture(Target target,double x,double y,double width,double height) {
        if(target==null||!ValidRoi(x,y,width,height))throw new ArgumentException("TITLE_ROI_INVALID");
        return CaptureRectangle(target,x,y,width,height);
    }
    public static Bitmap CaptureProbe(Target target) {
        var probe=CalibrationProbe(target);
        return CaptureRectangle(target,probe.X,probe.Y,probe.Width,probe.Height);
    }
    static void AssertUncovered(Target target,int sx,int sy,int w,int h) {
        // Retain the original point guards, and reject even narrow covering
        // windows between those points. EnumWindows visits top-level Z order.
        bool covered=false,reached=false;
        var patch=new Rectangle(target.Bounds.Left+sx,target.Bounds.Top+sy,w,h);
        EnumProc visit=delegate(IntPtr hwnd,IntPtr extra) {
            if(hwnd==target.Hwnd){reached=true;return false;}
            if(!IsWindowVisible(hwnd)||IsIconic(hwnd))return true;
            int cloaked;if(DwmGetWindowAttribute(hwnd,14,out cloaked,4)==0&&cloaked!=0)return true;
            Rect rect;
            if(!GetWindowRect(hwnd,out rect)){covered=true;return false;}
            if(patch.IntersectsWith(Rectangle.FromLTRB(rect.Left,rect.Top,rect.Right,rect.Bottom))){covered=true;return false;}
            return true;
        };
        EnumWindows(visit,IntPtr.Zero);GC.KeepAlive(visit);
        if(covered||!reached)throw new InvalidOperationException("TITLE_OCCLUDED");
        foreach(int dx in new[]{1,w/2,w-2})foreach(int dy in new[]{1,h/2,h-2}){
            var point=new Point{X=target.Bounds.Left+sx+dx,Y=target.Bounds.Top+sy+dy};
            if(GetAncestor(WindowFromPoint(point),2)!=target.Hwnd)throw new InvalidOperationException("TITLE_OCCLUDED");
        }
    }
    static Bitmap CaptureRectangle(Target target,double x,double y,double width,double height) {
        if(!StillCurrent(target))throw new InvalidOperationException("TITLE_TARGET_CHANGED");
        double scale=target.Dpi/96.0;
        int sx=(int)Math.Round(x*scale),sy=(int)Math.Round(y*scale),w=(int)Math.Round(width*scale),h=(int)Math.Round(height*scale);
        if(sx+w>target.Bounds.Right-target.Bounds.Left-8 || sy+h>target.Bounds.Bottom-target.Bounds.Top)throw new ArgumentException("TITLE_ROI_OUTSIDE");
        AssertUncovered(target,sx,sy,w,h);
        var bitmap=new Bitmap(w,h,System.Drawing.Imaging.PixelFormat.Format32bppArgb);
        IntPtr source=GetWindowDC(target.Hwnd);
        if(source==IntPtr.Zero){bitmap.Dispose();throw new InvalidOperationException("TITLE_CAPTURE_UNAVAILABLE");}
        try {
            using(var graphics=Graphics.FromImage(bitmap)){
                IntPtr destination=graphics.GetHdc();
                try {if(!BitBlt(destination,0,0,w,h,source,sx,sy,0x00CC0020))throw new InvalidOperationException("TITLE_CAPTURE_UNAVAILABLE");}
                finally {graphics.ReleaseHdc(destination);}
            }
            if(!StillCurrent(target))throw new InvalidOperationException("TITLE_TARGET_CHANGED");
            AssertUncovered(target,sx,sy,w,h);
            return bitmap;
        } catch {bitmap.Dispose();throw;}
        finally {ReleaseDC(target.Hwnd,source);}
    }
}
