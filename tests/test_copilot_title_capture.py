"""Execute the real C# capture flow against invented Win32 observations only.

Only native declarations and process queries are substituted before compilation.
No production branch, geometry calculation, guard, or disposal path is replaced.
Compilation fails closed if a real P/Invoke or process query remains. The fake
BitBlt writes to an existing memory-bitmap HDC; it never reads a desktop/window.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'scripts' / 'copilot_title_native.cs'


def synthetic_native(source):
    declaration = re.compile(r'\[DllImport\([^\n]+\)\]\s*static extern (\w+) (\w+)\(([^;]*)\);')

    def replace(match):
        result, name, parameters = match.groups()
        if name == 'EnumWindows':
            call = 'CaptureFixture.Enumerate(delegate(IntPtr hwnd){return callback(hwnd,extra);})'
        else:
            args = []
            for parameter in parameters.split(','):
                if not parameter.strip():
                    continue
                words = parameter.strip().split()
                args.append(('out ' if words[0] == 'out' else 'ref ' if words[0] == 'ref' else '') + words[-1])
            call = 'CaptureFixture.' + name + '(' + ','.join(args) + ')'
        return 'static ' + result + ' ' + name + '(' + parameters + '){return ' + call + ';}'

    transformed, count = declaration.subn(replace, source)
    if count < 17 or 'DllImport' in transformed:
        raise AssertionError('Native declarations were not completely isolated')
    transformed = transformed.replace('Process.GetProcessById(', 'CaptureFixture.GetProcessById(')
    if 'Process.Get' in transformed:
        raise AssertionError('Process queries were not completely isolated')
    transformed = transformed.replace('new Bitmap(w,h,', 'CaptureFixture.NewBitmap(w,h,')
    return transformed


HARNESS = r'''
public sealed class FixtureProcess : IDisposable {
    public string ProcessName { get { return "Weixin"; } }
    public FixtureProcess MainModule { get { return this; } }
    public FixtureProcess FileVersionInfo { get { return this; } }
    public string FileVersion { get { return CaptureFixture.Changed("version")?"4.2.0.0":"4.1.13.65"; } }
    public DateTime StartTime { get { return new DateTime(CaptureFixture.Changed("lifetime")?2:1,DateTimeKind.Utc); } }
    public void Dispose() {}
}
public static class CaptureFixture {
    public static CopilotHeaderNative.Rect Visual,Outer,Monitor;
    public static uint Dpi=96;
    public static string Fault="";
    public static bool After;
    public static int Blits,Acquired,Released,Restored;
    public static IntPtr ReleaseWindow,Context=new IntPtr(-1);
    public static Bitmap LastBitmap;
    public static Rectangle Copied,Title;
    public static void Reset() {
        Visual=new CopilotHeaderNative.Rect{Left=100,Top=100,Right=1100,Bottom=800};
        Outer=new CopilotHeaderNative.Rect{Left=92,Top=100,Right=1108,Bottom=808};
        Monitor=new CopilotHeaderNative.Rect{Left=0,Top=0,Right=1920,Bottom=1080};
        Dpi=96;Fault="";After=false;Blits=Acquired=Released=Restored=0;
        ReleaseWindow=new IntPtr(-1);Context=new IntPtr(-1);LastBitmap=null;Copied=Rectangle.Empty;
        Title=new Rectangle(404,136,480,40);
    }
    public static bool Changed(string name){return Fault==name+"-before" || After&&Fault==name+"-after";}
    public static bool Enumerate(Func<IntPtr,bool> visit) {
        if(Changed("occlusion")&&!visit(new IntPtr(84)))return true;
        if(!visit(new IntPtr(42)))return true;
        if(Changed("multiple"))visit(new IntPtr(43));
        return !Changed("enumerate");
    }
    public static FixtureProcess GetProcessById(int pid){return new FixtureProcess();}
    public static bool IsWindowVisible(IntPtr hwnd){return !Changed("hidden");}
    public static bool IsIconic(IntPtr hwnd){return Changed("minimized");}
    public static IntPtr GetWindow(IntPtr hwnd,uint command){return IntPtr.Zero;}
    public static IntPtr GetAncestor(IntPtr hwnd,uint flags){return hwnd;}
    public static IntPtr GetForegroundWindow(){return new IntPtr(Changed("foreground")?99:42);}
    public static uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid){pid=(uint)hwnd.ToInt64();if(Changed("pid")&&pid==42)pid=77;return 1;}
    public static bool GetWindowRect(IntPtr hwnd,out CopilotHeaderNative.Rect rect){
        rect=Outer;if(Changed("move"))rect.Left++;if(Changed("top-border"))rect.Top-=8;return !Changed("outer-rect");
    }
    public static int GetClassName(IntPtr hwnd,StringBuilder name,int count){name.Append(hwnd==new IntPtr(84)?"SyntheticOverlay":"mmui::MainWindow");return name.Length;}
    public static IntPtr GetWindowLongPtr(IntPtr hwnd,int field){return new IntPtr(field==-16?0x40000:0);}
    public static uint GetDpiForWindow(IntPtr hwnd){return Changed("dpi")?Dpi+24:Dpi;}
    public static IntPtr SetThreadDpiAwarenessContext(IntPtr value){
        if(Fault=="dpi-context")return IntPtr.Zero;
        if(Fault=="dpi-restore"&&value==new IntPtr(-1)){Restored++;return IntPtr.Zero;}
        IntPtr previous=Context;Context=value;if(value==new IntPtr(-1))Restored++;return previous;
    }
    public static uint GetCurrentThreadId(){return 1;}
    public static IntPtr GetThreadDesktop(uint thread){return new IntPtr(7);}
    public static bool GetUserObjectInformation(IntPtr desktop,int index,out int value,int length,out int needed){
        if(index!=6)throw new Exception("NON_NUMERIC_DESKTOP_QUERY");needed=4;value=Changed("lock")?0:1;return !Changed("desktop-error");
    }
    public static IntPtr WindowFromPoint(CopilotHeaderNative.Point point){return new IntPtr(Changed("occlusion")?84:42);}
    public static IntPtr GetWindowDC(IntPtr hwnd){Acquired++;return new IntPtr(1001);}
    public static IntPtr GetDC(IntPtr hwnd){if(hwnd!=IntPtr.Zero)throw new Exception("NON_DESKTOP_SOURCE");Acquired++;return Fault=="get-dc"?IntPtr.Zero:new IntPtr(1002);}
    public static int ReleaseDC(IntPtr hwnd,IntPtr dc){Released++;ReleaseWindow=hwnd;return Fault=="release-dc"?0:1;}
    public static Bitmap NewBitmap(int w,int h,System.Drawing.Imaging.PixelFormat format){LastBitmap=new Bitmap(w,h,format);return LastBitmap;}
    public static bool BitBlt(IntPtr dst,int x,int y,int w,int h,IntPtr src,int sx,int sy,uint operation){
        Blits++;Copied=new Rectangle(sx,sy,w,h);
        if(x!=0||y!=0||operation!=0x00CC0020)throw new Exception("UNBOUNDED_OR_CHANGED_COPY");
        using(var g=Graphics.FromHdc(dst)){
            g.Clear(src==new IntPtr(1001)?Color.Black:Color.White);
            if(src==new IntPtr(1002)){
                // Synthetic composited desktop: only the designated title is
                // painted; everything outside it is a different solid color.
                g.Clear(Color.Magenta);
                var title=Title;
                g.FillRectangle(Brushes.White,title.X-sx,title.Y-sy,title.Width,title.Height);
                g.FillRectangle(Brushes.Black,title.X-sx+12,title.Y-sy+10,30,16);
            }
        }
        After=true;return Fault!="bitblt";
    }
    public static int DwmGetWindowAttribute(IntPtr hwnd,int attr,out int value,int size){value=Changed("cloaked")?1:0;return 0;}
    public static int DwmGetWindowAttribute(IntPtr hwnd,int attr,out CopilotHeaderNative.Rect value,int size){
        value=Visual;if(Changed("move"))value.Left++;return Changed("dwm")?-1:0;
    }
    public static IntPtr MonitorFromRect(ref CopilotHeaderNative.Rect rect,uint flags){return new IntPtr(8);}
    internal static bool GetMonitorInfo(IntPtr monitor,ref CopilotHeaderNative.MonitorInfo info){
        info.Monitor=Monitor;info.Work=Monitor;return !Changed("monitor-error");
    }
}
public static class CaptureCases {
    static void Check(bool value,string reason){if(!value)throw new Exception(reason);}
    static CopilotHeaderNative.Target Target(){
        string key;using(var hash=SHA256.Create())key=BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes("42:42:1"))).Replace("-","").ToLowerInvariant();
        return new CopilotHeaderNative.Target{Hwnd=new IntPtr(42),Pid=42,Started=1,
            Dpi=CaptureFixture.Dpi,Bounds=CaptureFixture.Visual,AssistantPid=21,Key=key};
    }
    static void Disposed(){if(CaptureFixture.LastBitmap==null)return;bool failed=false;try{CaptureFixture.LastBitmap.GetPixel(0,0);}catch{failed=true;}Check(failed,"FAILED_BITMAP_RETAINED");}
    static void ExpectFailure(string reason){
        bool rejected=false;
        try{using(var image=CopilotHeaderNative.CaptureProbe(Target())){}}
        catch(Exception error){rejected=error.Message==reason;}
        Check(rejected,"EXPECTED_"+reason);Disposed();
        if(CaptureFixture.Fault=="dpi-restore")Check(CaptureFixture.Restored==1,"DPI_RESTORE_NOT_ATTEMPTED");
        else Check(CaptureFixture.Context==new IntPtr(-1),"DPI_CONTEXT_NOT_RESTORED");
    }
    public static void Run(string name){
        CaptureFixture.Reset();
        if(name=="source"){
            CaptureFixture.Outer=CaptureFixture.Visual;
            using(var image=CopilotHeaderNative.CaptureProbe(Target())){
                Check(image.Width==480&&image.Height==40,"CAPTURE_EXTENT_CHANGED");
                Check(image.GetPixel(0,0).ToArgb()==Color.White.ToArgb(),"COMPOSITED_TITLE_NOT_CAPTURED");
                Check(image.GetPixel(20,18).ToArgb()==Color.Black.ToArgb(),"TITLE_INK_MISSING");
                Check(CaptureFixture.Copied==new Rectangle(404,136,480,40),"DESKTOP_SOURCE_COORDINATES_WRONG");
            }
            Check(CaptureFixture.Blits==1&&CaptureFixture.Released==1&&CaptureFixture.ReleaseWindow==IntPtr.Zero,"DESKTOP_DC_NOT_RELEASED");
        }else if(name=="visual"){
            var target=CopilotHeaderNative.Find(21);
            Check(target!=null&&target.Bounds.Left==100&&target.Bounds.Top==100&&target.Bounds.Right==1100,"VISUAL_BOUNDS_REQUIRED");
            Check(CaptureFixture.Context==new IntPtr(-1),"FIND_DPI_CONTEXT_NOT_RESTORED");
        }else if(name=="geometry"){
            int[][] vectors={new[]{96,-2196,-264,480,40},new[]{120,-2120,-255,600,50},
                new[]{144,-2044,-246,720,60},new[]{192,-1892,-228,960,80},new[]{288,-1588,-192,1440,120}};
            foreach(var vector in vectors){
                uint dpi=(uint)vector[0];
                CaptureFixture.Reset();CaptureFixture.Dpi=dpi;double scale=dpi/96.0;
                CaptureFixture.Visual=new CopilotHeaderNative.Rect{Left=-2500,Top=-300,Right=-2500+(int)(1000*scale),Bottom=-300+(int)(700*scale)};
                CaptureFixture.Outer=CaptureFixture.Visual;CaptureFixture.Outer.Left-=8;CaptureFixture.Outer.Right+=8;CaptureFixture.Outer.Bottom+=8;
                CaptureFixture.Monitor=new CopilotHeaderNative.Rect{Left=-3000,Top=-1000,Right=1000,Bottom=2000};
                CaptureFixture.Title=new Rectangle(vector[1],vector[2],vector[3],vector[4]);
                using(var image=CopilotHeaderNative.CaptureProbe(Target())){
                    Check(CaptureFixture.Copied==CaptureFixture.Title,"PHYSICAL_DPI_RECT_WRONG");
                    Check(image.GetPixel(0,0).ToArgb()==Color.White.ToArgb(),"BORDER_OR_ORIGIN_SHIFT");
                }
            }
        }else if(name=="manual"){
            using(var image=CopilotHeaderNative.Capture(Target(),350,44,120,24)){
                Check(image.Width==120&&image.Height==24&&CaptureFixture.Copied==new Rectangle(450,144,120,24),"MANUAL_VISUAL_ROI_CHANGED");
            }
        }else if(name=="offscreen"){
            CaptureFixture.Monitor.Right=600;ExpectFailure("TITLE_OCCLUDED");Check(CaptureFixture.Blits==0,"OFFSCREEN_COPY_OCCURRED");
        }else if(name=="invalid"){
            bool rejected=false;try{CopilotHeaderNative.Capture(Target(),0,0,1920,1080);}catch{rejected=true;}
            Check(rejected&&CaptureFixture.Blits==0&&CaptureFixture.Acquired==0,"INVALID_ROI_ACQUIRED");
        }else if(name=="dwm-find"){
            CaptureFixture.Fault="dwm-before";Check(CopilotHeaderNative.Find(21)==null,"DWM_FAILURE_FELL_BACK_TO_OUTER_BOUNDS");
        }else{
            CaptureFixture.Fault=name;
            string reason=name.StartsWith("lock")||name.StartsWith("desktop-error")?"TITLE_WINDOW_UNAVAILABLE":
                name=="dpi-context"||name=="dpi-restore"?"TITLE_DPI_UNAVAILABLE":name.StartsWith("occlusion")||name.StartsWith("monitor-error")?"TITLE_OCCLUDED":
                name=="get-dc"||name=="bitblt"||name=="release-dc"?"TITLE_CAPTURE_UNAVAILABLE":"TITLE_TARGET_CHANGED";
            ExpectFailure(reason);
            if(name.EndsWith("-before")||name=="dpi-context")Check(CaptureFixture.Blits==0,"PRECHECK_COPIED_PIXELS");
            if(name.EndsWith("-after"))Check(CaptureFixture.Blits==1,"POSTCHECK_NOT_EXERCISED");
            if(CaptureFixture.Acquired>0&&name!="get-dc")Check(CaptureFixture.Released==1&&CaptureFixture.ReleaseWindow==IntPtr.Zero,"FAILED_CAPTURE_LEAKED_DC");
        }
        if(name!="dpi-restore")Check(CaptureFixture.Context==new IntPtr(-1),"CAPTURE_DPI_CONTEXT_NOT_RESTORED");
    }
}
'''


@unittest.skipUnless(os.name == 'nt', 'Windows .NET drawing runtime required')
class HeaderCaptureTests(unittest.TestCase):
    def run_case(self, case):
        source = synthetic_native(NATIVE.read_text(encoding='utf-8'))
        # Older source has no monitor structure; the declaration is a fixture
        # compatibility shim only, allowing the original implementation to RED.
        if 'struct MonitorInfo' not in source:
            source = source.replace('public static class CopilotHeaderNative {',
                'public static class CopilotHeaderNative { public struct MonitorInfo { public int Size; public Rect Monitor,Work; public uint Flags; }')
        command = "$ErrorActionPreference='Stop'; $p=[Console]::In.ReadToEnd()|ConvertFrom-Json; Add-Type -TypeDefinition ($p.native+$p.harness) -ReferencedAssemblies System.Drawing; [CaptureCases]::Run($p.case)"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            input=json.dumps({'native': source, 'harness': HARNESS, 'case': case}),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-1800:])

    def test_composited_title_patch_replaces_black_window_surface(self):
        self.run_case('source')

    def test_find_uses_visual_bounds_and_restores_dpi(self):
        self.run_case('visual')

    def test_physical_crop_handles_dpi_negative_origins_and_invisible_borders(self):
        self.run_case('geometry')
        self.run_case('manual')

    def test_dwm_failure_never_reinterprets_roi_in_outer_window_coordinates(self):
        self.run_case('dwm-find')

    def test_changed_top_border_cannot_shift_probe_into_body(self):
        for case in ('top-border-before', 'top-border-after', 'outer-rect-before', 'outer-rect-after'):
            with self.subTest(case=case):
                self.run_case(case)

    def test_preconditions_prevent_acquisition_and_changes_discard_after_copy(self):
        for cause in ('lock', 'desktop-error', 'move', 'pid', 'dpi', 'hidden', 'minimized', 'multiple', 'enumerate',
                      'foreground', 'cloaked', 'lifetime', 'version', 'dwm', 'occlusion', 'monitor-error'):
            for phase in ('before', 'after'):
                with self.subTest(cause=cause, phase=phase):
                    self.run_case(cause + '-' + phase)

    def test_invalid_and_offscreen_regions_never_copy(self):
        for case in ('invalid', 'offscreen'):
            with self.subTest(case=case):
                self.run_case(case)

    def test_acquisition_failures_release_resources_and_never_return_bitmap(self):
        for case in ('dpi-context', 'dpi-restore', 'get-dc', 'bitblt', 'release-dc'):
            with self.subTest(case=case):
                self.run_case(case)


if __name__ == '__main__':
    unittest.main()
