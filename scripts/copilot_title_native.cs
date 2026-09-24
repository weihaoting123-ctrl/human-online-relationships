// Read only a user-calibrated header patch. No focus, input, injection or disk I/O.
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
    public sealed class Target { public IntPtr Hwnd; public Rect Bounds; public uint Dpi,Pid; public long Started; public string Key; }
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
                    candidates.Add(new Target{Hwnd=hwnd,Bounds=rect,Dpi=dpi,Pid=pid,Started=process.StartTime.ToUniversalTime().Ticks,Key=key});
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
            using(var process=Process.GetProcessById((int)pid))return process.StartTime.ToUniversalTime().Ticks==target.Started;
        }catch{return false;}
    }
    public static Bitmap Capture(Target target,double x,double y,double width,double height) {
        if(target==null||!ValidRoi(x,y,width,height))throw new ArgumentException("TITLE_ROI_INVALID");
        if(!StillCurrent(target))throw new InvalidOperationException("TITLE_TARGET_CHANGED");
        double scale=target.Dpi/96.0;
        int sx=(int)Math.Round(x*scale),sy=(int)Math.Round(y*scale),w=(int)Math.Round(width*scale),h=(int)Math.Round(height*scale);
        if(sx+w>target.Bounds.Right-target.Bounds.Left-8 || sy+h>target.Bounds.Bottom-target.Bounds.Top)throw new ArgumentException("TITLE_ROI_OUTSIDE");
        // Do not accidentally read covering applications or a clipped/offscreen patch.
        foreach(int dx in new[]{1,w/2,w-2})foreach(int dy in new[]{1,h/2,h-2}){
            var point=new Point{X=target.Bounds.Left+sx+dx,Y=target.Bounds.Top+sy+dy};
            if(GetAncestor(WindowFromPoint(point),2)!=target.Hwnd)throw new InvalidOperationException("TITLE_OCCLUDED");
        }
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
            return bitmap;
        } catch {bitmap.Dispose();throw;}
        finally {ReleaseDC(target.Hwnd,source);}
    }
}
