$ErrorActionPreference = 'Stop'
# Production prewarmed terminal helper (dev spec ?5.3.4, VS-52). No input is injected or swallowed.
# All native callbacks and command handling run on the same message-loop thread.
$source = @'
using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

public static class NativeTransferTerminalInput {
    const uint CommandMessage = 0x8001;
    const uint WM_LBUTTONUP = 0x0202, WM_KEYDOWN = 0x0100, WM_SYSKEYDOWN = 0x0104;
    const uint EVENT_DESTROY = 0x8001, EVENT_SHOW = 0x8002, EVENT_HIDE = 0x8003, EVENT_LOCATION = 0x800B;
    static readonly ConcurrentQueue<string> commands = new ConcurrentQueue<string>();
    static readonly BlockingCollection<string> output = new BlockingCollection<string>();
    static readonly JavaScriptSerializer json = new JavaScriptSerializer();
    static readonly Dictionary<string, Saved> records = new Dictionary<string, Saved>();
    static readonly object outputLock = new object();
    static readonly HookProc mouseCallback = Mouse, keyboardCallback = Keyboard;
    static readonly WinEventProc windowCallback = WindowEvent;
    static uint threadId;
    static IntPtr mouseHook, keyboardHook, windowHook;
    static string armed;
    static bool stopping;
    static long generation;
    static Exception fatal;

    [StructLayout(LayoutKind.Sequential)] public struct Point { public int x, y; }
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential)] struct MouseData { public Point pt; public uint mouseData, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct KeyboardData { public uint vkCode, scanCode, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct Msg { public IntPtr hwnd; public uint message; public UIntPtr wParam; public IntPtr lParam; public uint time; public Point pt; public uint lPrivate; }
    public sealed class Target {
        public string hwnd; public uint pid; public string className; public Rect rect;
    }
    public sealed class Terminal {
        public string @event = "terminal", token, kind, provenance;
        public uint tick;
        public bool injected;
        public Point? point;
        public Target target;
        public long topologyGeneration;
    }
    // Terminal snapshots are never modified. Invalidation metadata is separate.
    sealed class Topology { public string hwnd; public uint evt; }
    sealed class Saved {
        public Terminal terminal;
        public List<Topology> changes = new List<Topology>();
        public HashSet<string> prepared = new HashSet<string>();
    }
    delegate IntPtr HookProc(int code, IntPtr wParam, IntPtr lParam);
    delegate void WinEventProc(IntPtr hook, uint evt, IntPtr hwnd, int objectId, int childId, uint eventThread, uint tick);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SetWindowsHookEx(int hook, HookProc callback, IntPtr module, uint thread);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SetWinEventHook(uint first, uint last, IntPtr module, WinEventProc callback, uint process, uint thread, uint flags);
    [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr hook);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] static extern IntPtr WindowFromPhysicalPoint(Point point);
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder text, int size);
    [DllImport("user32.dll", SetLastError=true)] static extern int GetMessage(out Msg msg, IntPtr hwnd, uint first, uint last);
    [DllImport("user32.dll")] static extern bool PeekMessage(out Msg msg, IntPtr hwnd, uint first, uint last, uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref Msg msg);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref Msg msg);
    [DllImport("user32.dll", SetLastError=true)] static extern bool PostThreadMessage(uint thread, uint msg, UIntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] static extern void PostQuitMessage(int exitCode);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr GetModuleHandle(string name);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll")] static extern uint GetTickCount();

    static void Emit(object value) { output.Add(json.Serialize(value)); }
    static void Fail(Exception ex) {
        Interlocked.CompareExchange(ref fatal, ex, null);
        PostThreadMessage(threadId, 0x0012, UIntPtr.Zero, IntPtr.Zero);
    }
    static Target Capture(Point point) {
        IntPtr hwnd = WindowFromPhysicalPoint(point);
        if (hwnd == IntPtr.Zero) return null;
        hwnd = GetAncestor(hwnd, 2); // GA_ROOT: never infer a later target from the cursor.
        Rect rect; uint pid;
        StringBuilder name = new StringBuilder(512);
        if (hwnd == IntPtr.Zero || !IsWindow(hwnd) || !GetWindowRect(hwnd, out rect) ||
            GetWindowThreadProcessId(hwnd, out pid) == 0 || GetClassName(hwnd, name, name.Capacity) == 0) return null;
        return new Target { hwnd = hwnd.ToInt64().ToString(CultureInfo.InvariantCulture), pid = pid, className = name.ToString(), rect = rect };
    }
    static Terminal Save(string token, string kind, uint tick, Point? point, string provenance) {
        Terminal record = new Terminal { token = token, kind = kind, tick = tick, point = point,
            target = point.HasValue ? Capture(point.Value) : null, provenance = provenance, topologyGeneration = generation };
        records.Add(token, new Saved { terminal = record });
        return record;
    }
    static IntPtr Mouse(int code, IntPtr wParam, IntPtr lParam) {
        try {
            if (code >= 0 && wParam.ToInt64() == WM_LBUTTONUP && armed != null) {
                MouseData data = (MouseData)Marshal.PtrToStructure(lParam, typeof(MouseData));
                string token = armed; armed = null;
                // Capture synchronously inside WH_MOUSE_LL, BEFORE CallNextHookEx.
                Terminal record = Save(token, "up", data.time, data.pt, "native-hook");
                record.injected = (data.flags & 1) != 0;
                Emit(record);
            }
        } catch (Exception ex) { Fail(ex); }
        return CallNextHookEx(mouseHook, code, wParam, lParam);
    }
    static IntPtr Keyboard(int code, IntPtr wParam, IntPtr lParam) {
        try {
            if (code >= 0 && armed != null && (wParam.ToInt64() == WM_KEYDOWN || wParam.ToInt64() == WM_SYSKEYDOWN)) {
                KeyboardData data = (KeyboardData)Marshal.PtrToStructure(lParam, typeof(KeyboardData));
                if (data.vkCode == 0x1B) {
                    string token = armed; armed = null;
                    // Keyboard cancellation has no mouse coordinate; do not invent/poll one.
                    Terminal record = Save(token, "cancel", data.time, null, "native-hook");
                    record.injected = (data.flags & 0x10) != 0;
                    Emit(record);
                }
            }
        } catch (Exception ex) { Fail(ex); }
        return CallNextHookEx(keyboardHook, code, wParam, lParam);
    }
    static void WindowEvent(IntPtr hook, uint evt, IntPtr hwnd, int objectId, int childId, uint eventThread, uint tick) {
        try {
            if (objectId != 0 || childId != 0 || (evt != EVENT_DESTROY && evt != EVENT_SHOW && evt != EVENT_HIDE && evt != EVENT_LOCATION)) return;
            generation++;
            foreach (Saved saved in records.Values) {
                // DWORD wrap-safe ordering; queued events older than the terminal do not invalidate it.
                if (unchecked((int)(tick - saved.terminal.tick)) < 0) continue;
                IntPtr root = GetAncestor(hwnd, 2);
                bool captured = saved.terminal.target != null && saved.terminal.target.hwnd == hwnd.ToInt64().ToString(CultureInfo.InvariantCulture);
                // A SHOW queued until after HIDE/DESTROY is STILL a change. Never consult
                // delivery-time visibility to discard history. Unknown destroyed HWNDs
                // conservatively invalidate too; live child controls are not top-level.
                if (!captured && root != IntPtr.Zero && root != hwnd) continue;
                string key = hwnd.ToInt64().ToString(CultureInfo.InvariantCulture);
                if (saved.prepared.Contains(key)) {
                    // Preparation is an exemption for a NEVER-SHOWN hidden shell,
                    // not immunity: a later SHOW/DESTROY permanently revokes it.
                    if (evt != EVENT_SHOW && evt != EVENT_DESTROY) continue;
                    saved.prepared.Remove(key);
                }
                saved.changes.Add(new Topology { hwnd = key, evt = evt });
            }
        } catch (Exception ex) { Fail(ex); }
    }
    static bool Same(Target a, Target b) {
        return a != null && b != null && a.hwnd == b.hwnd && a.pid == b.pid && a.className == b.className &&
            a.rect.left == b.rect.left && a.rect.top == b.rect.top && a.rect.right == b.rect.right && a.rect.bottom == b.rect.bottom;
    }
    static string Token(Dictionary<string, object> command) {
        object value;
        if (!command.TryGetValue("token", out value) || !(value is string) || String.IsNullOrEmpty((string)value)) throw new ArgumentException("token must be a nonempty string");
        return (string)value;
    }
    static int Coordinate(Dictionary<string, object> command, string key) {
        object value;
        if (!command.TryGetValue(key, out value) || !(value is int)) throw new ArgumentException(key + " must be an Int32");
        return (int)value;
    }
    static void Handle(string line) {
        object id = null;
        try {
            Dictionary<string, object> command = json.Deserialize<Dictionary<string, object>>(line);
            if (command == null || !command.TryGetValue("id", out id)) throw new ArgumentException("id is required");
            object operation;
            if (!command.TryGetValue("op", out operation) || !(operation is string)) throw new ArgumentException("op is required");
            string op = (string)operation;
            if (op == "quit") { armed = null; records.Clear(); stopping = true; Emit(new { id, ok = true }); PostQuitMessage(0); return; }
            if (op == "cancel") {
                object cancelled;
                if (command.TryGetValue("token", out cancelled) && cancelled is string) {
                    if (armed == (string)cancelled) armed = null;
                    records.Remove((string)cancelled);
                } else { armed = null; records.Clear(); }
                Emit(new { id, ok = true }); return;
            }
            string token = Token(command);
            if (op == "arm") {
                object synthetic;
                if (!command.TryGetValue("synthetic", out synthetic) || !(synthetic is bool) || (bool)synthetic) throw new ArgumentException("arm requires synthetic:false; use sample for tests");
                if (armed != null) throw new InvalidOperationException("already armed");
                if (records.ContainsKey(token)) throw new InvalidOperationException("token already has a terminal; consume it first");
                if ((GetAsyncKeyState(1) & 0x8000) == 0) throw new InvalidOperationException("left button is not down");
                armed = token; Emit(new { id, ok = true });
            } else if (op == "prepare") {
                Saved saved;
                if (!records.TryGetValue(token, out saved)) throw new InvalidOperationException("unknown-token");
                string key = Convert.ToString(command["hwnd"], CultureInfo.InvariantCulture);
                IntPtr handle = new IntPtr(Int64.Parse(key, CultureInfo.InvariantCulture));
                uint pid;
                GetWindowThreadProcessId(handle, out pid);
                if (!IsWindow(handle) || IsWindowVisible(handle) || pid != Convert.ToUInt32(command["pid"]) ||
                    (saved.terminal.target != null && saved.terminal.target.hwnd == key)) throw new InvalidOperationException("not a hidden prepared window");
                foreach (Topology change in saved.changes) if (change.hwnd == key && change.evt == EVENT_SHOW)
                    throw new InvalidOperationException("prepared window was shown after terminal");
                saved.changes.RemoveAll(delegate(Topology change) { return change.hwnd == key; });
                saved.prepared.Add(key); Emit(new { id, ok = true });
            } else if (op == "validate" || op == "finish") {
                Saved saved; Target current = null; string reason;
                if (!records.TryGetValue(token, out saved)) reason = "unknown-token";
                else {
                    Terminal record = saved.terminal;
                    if (record.point.HasValue) current = Capture(record.point.Value);
                    IntPtr captured = record.target == null ? IntPtr.Zero : new IntPtr(Int64.Parse(record.target.hwnd, CultureInfo.InvariantCulture));
                    if (record.kind != "up") reason = "cancelled";
                    else if (saved.changes.Count != 0) reason = "window-topology-changed";
                    else if (record.target == null) reason = "no-captured-target";
                    else if (!IsWindow(captured)) reason = "captured-window-destroyed";
                    else if (!Same(record.target, current)) reason = "target-signature-changed";
                    else reason = "unchanged";
                }
                // finish is the linearization point: validate + one-shot consume on
                // this single native message-loop thread, not a fire-and-forget ACK.
                if (op == "finish") records.Remove(token);
                Emit(new { id, ok = true, valid = reason == "unchanged", reason, target = current, consumed = op == "finish" });
            } else if (op == "consume") {
                if (armed == token) armed = null;
                bool removed = records.Remove(token);
                Emit(new { id, ok = true, removed });
            } else throw new ArgumentException("unknown op: " + op);
        } catch (Exception ex) { Emit(new { id, ok = false, error = ex.Message }); }
    }
    public static void Run() {
        Thread writer = null;
        IntPtr priorDpi = IntPtr.Zero;
        try {
            threadId = GetCurrentThreadId();
            Msg msg; PeekMessage(out msg, IntPtr.Zero, 0, 0, 0); // Create queue before stdin thread posts.
            priorDpi = SetThreadDpiAwarenessContext(new IntPtr(-4)); // PMv2 keeps GetWindowRect physical too.
            if (priorDpi == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot select physical DPI context");
            writer = new Thread(delegate() {
                try {
                    using (StreamWriter stream = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false))) {
                        foreach (string line in output.GetConsumingEnumerable()) lock (outputLock) { stream.WriteLine(line); stream.Flush(); }
                    }
                } catch (Exception ex) { Fail(ex); }
            });
            writer.IsBackground = true; writer.Start();
            mouseHook = SetWindowsHookEx(14, mouseCallback, GetModuleHandle(null), 0);
            if (mouseHook == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "WH_MOUSE_LL installation failed");
            keyboardHook = SetWindowsHookEx(13, keyboardCallback, GetModuleHandle(null), 0);
            if (keyboardHook == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "WH_KEYBOARD_LL installation failed");
            windowHook = SetWinEventHook(EVENT_DESTROY, EVENT_LOCATION, IntPtr.Zero, windowCallback, 0, 0, 0);
            if (windowHook == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "SetWinEventHook installation failed");
            Emit(new { @event = "ready", ready = true, provenance = "native-hook" });
            Thread reader = new Thread(delegate() {
                try {
                    string line;
                    while ((line = Console.ReadLine()) != null) {
                        commands.Enqueue(line);
                        if (!PostThreadMessage(threadId, CommandMessage, UIntPtr.Zero, IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                    PostThreadMessage(threadId, 0x0012, UIntPtr.Zero, IntPtr.Zero);
                } catch (Exception ex) { Fail(ex); }
            });
            reader.IsBackground = true; reader.Start();
            int result = 0;
            while (!stopping && (result = GetMessage(out msg, IntPtr.Zero, 0, 0)) > 0) {
                if (msg.message == CommandMessage) { string line; if (commands.TryDequeue(out line)) Handle(line); }
                else { TranslateMessage(ref msg); DispatchMessage(ref msg); }
            }
            if (result < 0) throw new Win32Exception(Marshal.GetLastWin32Error(), "GetMessage failed");
        } finally {
            if (mouseHook != IntPtr.Zero) UnhookWindowsHookEx(mouseHook);
            if (keyboardHook != IntPtr.Zero) UnhookWindowsHookEx(keyboardHook);
            if (windowHook != IntPtr.Zero) UnhookWinEvent(windowHook);
            if (priorDpi != IntPtr.Zero) SetThreadDpiAwarenessContext(priorDpi);
            output.CompleteAdding();
            if (writer != null && writer.IsAlive) writer.Join();
        }
        if (fatal != null) throw new InvalidOperationException("Native terminal helper failed", fatal);
    }
}
'@

try {
    Add-Type -TypeDefinition $source -ReferencedAssemblies System.Web.Extensions
    [NativeTransferTerminalInput]::Run()
} catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
    exit 1
}
