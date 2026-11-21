# OS-provided UI Automation. The request is JSON on stdin, independent of command parsing.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Fail([string]$code) { throw [System.InvalidOperationException]::new($code) }

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $session = [Diagnostics.Process]::GetCurrentProcess().SessionId
    if ($session -eq 0 -or ![Environment]::UserInteractive) { Fail 'NO_INTERACTIVE_DESKTOP' }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class XdevWindows {
    delegate bool Callback(IntPtr hwnd, IntPtr state);
    [DllImport("user32")] static extern bool EnumWindows(Callback callback, IntPtr state);
    [DllImport("user32")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32")] public static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("user32")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder value, int count);
    [DllImport("user32")] static extern IntPtr GetThreadDesktop(uint id);
    [DllImport("kernel32")] static extern uint GetCurrentThreadId();
    [DllImport("user32")] static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32")] static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32", CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr handle, int type, StringBuilder data, int size, out int needed);
    static string DesktopName(IntPtr handle) {
        var s = new StringBuilder(256); int needed;
        return handle == IntPtr.Zero || !GetUserObjectInformation(handle, 2, s, 512, out needed) ? "" : s.ToString();
    }
    public static bool OnInputDesktop() {
        var input = OpenInputDesktop(0, false, 1);
        if (input == IntPtr.Zero) return false;
        try {
            var name = DesktopName(input);
            return name.Length > 0 && name == DesktopName(GetThreadDesktop(GetCurrentThreadId()));
        } finally { CloseDesktop(input); }
    }
    public static long[] Windows() {
        var result = new List<long>();
        EnumWindows(delegate(IntPtr hwnd, IntPtr unused) {
            if (IsWindowVisible(hwnd)) result.Add(hwnd.ToInt64());
            return true;
        }, IntPtr.Zero);
        return result.ToArray();
    }
    public static int Owner(IntPtr hwnd) {
        uint pid; GetWindowThreadProcessId(hwnd, out pid); return (int)pid;
    }
    public static string Title(IntPtr hwnd) {
        var text = new StringBuilder(256); GetWindowText(hwnd,text,256); return text.ToString();
    }
}
'@
    if (![XdevWindows]::OnInputDesktop()) { Fail 'NO_INTERACTIVE_DESKTOP' }
    try { Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes } catch { Fail 'UI_AUTOMATION_UNAVAILABLE' }
    function WindowInfo([long]$number, [string]$appFilter) {
        $hwnd = [IntPtr]::new($number)
        if (![XdevWindows]::IsWindowVisible($hwnd) -or [XdevWindows]::GetAncestor($hwnd, 2) -ne $hwnd) { return }
        $ownerId = [XdevWindows]::Owner($hwnd)
        try {
            $process = [Diagnostics.Process]::GetProcessById($ownerId)
            if ($process.SessionId -ne $session) { return }
            $ownerName = $process.ProcessName
            if ($appFilter -and $ownerName -ine $appFilter) { return }
            $ticks = $process.StartTime.ToUniversalTime().Ticks
        } catch { return } finally { if ($process) { $process.Dispose() } }
        if ($ownerName -in @('CredentialUIBroker', 'LogonUI', 'consent')) { return }
        return @{
            target = 'win:{0}:{1}:{2}:{3}' -f $session,$ownerId,$ticks,$number
            app = $ownerName
            title = [XdevWindows]::Title($hwnd)
        }
    }
    function ShortText([string]$text) {
        if ($text.Length -gt 360) { return $text.Substring(0, 360) }
        return $text
    }
    function NodeRef([string]$parent, $index, $runtimeId, $kind, $id, $class, $label) {
        $identity = ConvertTo-Json -Compress -InputObject @($parent, $index, ($runtimeId -join '.'), $kind, $id, $class, $label)
        $bytes = [Text.Encoding]::UTF8.GetBytes($identity)
        return 'u:' + [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()
    }
    # Names can serve as values in poorly implemented providers. Redact the subtree before probing its patterns.
    $sensitive = '(?i)pass(?:word|phrase|wd|code)|pin(?:code|field|input)|\bpin\b|secret|token|one.?time|verification.?code|security.?code|api.?key|private.?key|credential|credit.?card|card.?number|cvv|cvc|\u5bc6\u7801|\u5bc6\u94a5|\u53e3\u4ee4|\u9a8c\u8bc1\u7801|\u4fe1\u7528\u5361'
    function DocumentExcerpt($element) {
        # A document provider can put an embedded password field's content into its
        # parent text range. Only aggregate a read-only text leaf with no embedded UI.
        $pattern = $null
        if (!$element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) { return }
        if ([System.Windows.Automation.TreeWalker]::RawViewWalker.GetFirstChild($element)) { return }
        $entire = $pattern.DocumentRange
        $readOnly = $entire.GetAttributeValue([System.Windows.Automation.TextPattern]::IsReadOnlyAttribute)
        if ($readOnly -isnot [bool] -or !$readOnly -or $entire.GetChildren().Count -ne 0) { return }
        # Providers with no visible range can still expose accessible text. GetText
        # always has a length limit. Never fetch a full string and truncate it later.
        $visible = $pattern.GetVisibleRanges()
        if ($visible.Count -gt 0) { $range = $visible[0]; $scope = 'visible' } else { $range = $entire; $scope = 'accessible' }
        $excerpt = $range.GetText(641)
        if ($excerpt) {
            return @{text=$excerpt.Substring(0,[Math]::Min(640,$excerpt.Length)); text_scope=$scope; text_truncated=($excerpt.Length -gt 640 -or $visible.Count -gt 1)}
        }
    }
    function Walk($element, [string]$parentRef, [int]$index, [int]$depth, [bool]$ancestorOffscreen) {
        if ($ctx.found -or $ctx.visited -ge 180) { $ctx.truncated = $true; return }
        $ctx.visited++
        $current = $element.Current
        if ($current.IsPassword) { return }
        $id = $current.AutomationId
        $name = $current.Name
        $class = $current.ClassName
        if ($id -match $sensitive -or $name -match $sensitive -or $class -match $sensitive) { return }
        $role = $current.ControlType.ProgrammaticName.Replace('ControlType.','')
        $enabled = $current.IsEnabled
        $offscreen = $ancestorOffscreen -or $current.IsOffscreen
        $ref = NodeRef $parentRef $index $element.GetRuntimeId() $role $id $class $name
        $actions = @()
        $invocation = $null
        $editable = $null
        if ($enabled -and $element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$invocation)) {
            $actions += 'invoke'
        }
        if ($enabled -and $role -eq 'Edit' -and
            $element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$editable) -and !$editable.Current.IsReadOnly) {
            $actions += 'set-value'
        }
        if ($request.action -eq 'snapshot') {
            $row = @{ref=$ref; depth=$depth; role=$role; name=(ShortText $name); enabled=$enabled; offscreen=$offscreen; actions=$actions}
            if ($role -eq 'Document') {
                # Older text providers can decline an attribute or a range.
                try { $excerpt = DocumentExcerpt $element; if ($excerpt) { $row += $excerpt } } catch {}
            }
            # Input values can be private even for ordinary edit controls. Do not read ValuePattern.Value.
            $ctx.nodes.Add($row)
        } elseif ($request.ref -ceq $ref) {
            if ($actions -notcontains $request.action) { Fail 'ACTION_UNSUPPORTED' }
            if ($element.Current.IsPassword -or !$element.Current.IsEnabled) { Fail 'STALE_REF' }
            if (![XdevWindows]::OnInputDesktop() -or $number -notin [XdevWindows]::Windows() -or
                (WindowInfo $number).target -cne $request.target) { Fail 'STALE_TARGET' }
            $ctx.started = $true
            if ($request.action -eq 'invoke') { $invocation.Invoke() } else { $editable.SetValue([string]$request.value) }
            $ctx.found = $true
            return
        }
        if ($role -eq 'Edit') { return }
        $child = $walker.GetFirstChild($element)
        if ($depth -ge 14 -and $child) { $ctx.truncated = $true; return }
        for ($i = 0; $null -ne $child; $i++) {
            if ($ctx.found) { return }
            if ($ctx.visited -ge 180) { $ctx.truncated = $true; return }
            Walk $child $ref $i ($depth+1) $offscreen
            if (!$ctx.found) { $child = $walker.GetNextSibling($child) } else { return }
        }
    }
    if ($request.action -eq 'targets') {
        $targets = [Collections.Generic.List[object]]::new()
        $truncated = $false
        $filter = $request.app -replace '\.exe$',''
        foreach ($number in [XdevWindows]::Windows()) {
            $item = WindowInfo $number $filter
            if (!$item -or !$item.title) { continue }
            if ($targets.Count -ge 100) { $truncated = $true; break }
            $targets.Add($item)
        }
        $result = @{ok=$true; backend='windows'; targets=$targets; truncated=$truncated}
    } else {
        $number = [long](($request.target -split ':')[4])
        if ($number -notin [XdevWindows]::Windows()) { Fail 'STALE_TARGET' }
        $item = WindowInfo $number
        if (!$item -or $item.target -cne $request.target) { Fail 'STALE_TARGET' }
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new($number))
        if ($root.Current.ProcessId -ne [XdevWindows]::Owner([IntPtr]::new($number))) { Fail 'STALE_TARGET' }
        $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        $sha = [Security.Cryptography.SHA256]::Create()
        $ctx = @{found=$false; started=$false; visited=0; truncated=$false; nodes=[Collections.Generic.List[object]]::new()}
        try { Walk $root $item.target 0 0 $false } finally { $sha.Dispose() }
        if ($request.action -eq 'snapshot') {
            $result = @{ok=$true; backend='windows'; target=$item.target; nodes=$ctx.nodes; truncated=$ctx.truncated}
        } elseif ($ctx.found) {
            $result = @{ok=$true; backend='windows'; target=$item.target; action=$request.action; status='dispatched'}
        } else { Fail 'STALE_REF' }
    }
} catch {
    $code = $_.Exception.Message
    if ($code -notin @('STALE_TARGET','STALE_REF','ACTION_UNSUPPORTED','NO_INTERACTIVE_DESKTOP','UI_AUTOMATION_UNAVAILABLE')) { $code = 'UI_PROVIDER_FAILED' }
    $result = @{ok=$false; error_code=$code}
    if ($ctx.started) { $result.outcome = 'unknown' }
}
[Console]::WriteLine((ConvertTo-Json -InputObject $result -Depth 8 -Compress))
