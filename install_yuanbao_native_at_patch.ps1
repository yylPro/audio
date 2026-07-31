[CmdletBinding()]
param(
    [string]$OpenClawCmd = "D:\OpenClaw\openclaw.cmd",
    [string]$ProjectDir = $PSScriptRoot,
    [string]$PluginRoot,
    [switch]$Restore,
    [switch]$SkipRestart
)

$ErrorActionPreference = "Stop"
$supportedVersion = "2.17.0"
$patchMarker = "YUANBAO_NATIVE_AT_PATCH_V1"
$discoveryMarker = "YUANBAO_MEMBER_DISCOVERY_V1"
$safetyMarker = "YUANBAO_NATIVE_AT_SAFETY_V1"
$relativeFiles = @(
    "dist\src\business\messaging\handlers\index.js",
    "dist\src\business\pipeline\middlewares\extract-content.js",
    "dist\src\business\actions\text\send.js",
    "dist\src\business\actions\deliver.js",
    "dist\src\infra\transport.js",
    "dist\src\access\ws\biz-codec.js",
    "dist\src\access\ws\proto\biz.json"
)

function Read-Utf8([string]$Path) {
    return [IO.File]::ReadAllText($Path, [Text.UTF8Encoding]::new($false))
}

function Write-Utf8([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

function Replace-Required([string]$Content, [string]$Old, [string]$New, [string]$Label) {
    if (-not $Content.Contains($Old)) {
        throw "Patch point not found: $Label. The plugin structure may have changed."
    }
    return $Content.Replace($Old, $New)
}

function Resolve-PluginRoot {
    if ($PluginRoot) {
        return (Resolve-Path -LiteralPath $PluginRoot).Path
    }
    if (-not (Test-Path -LiteralPath $OpenClawCmd)) {
        throw "OpenClaw command not found: $OpenClawCmd"
    }
    $raw = (& $OpenClawCmd plugins inspect yuanbao --runtime --json 2>&1 | Out-String)
    $start = $raw.IndexOf("{")
    if ($start -lt 0) {
        throw "Could not read Yuanbao plugin metadata: $raw"
    }
    $metadata = $raw.Substring($start) | ConvertFrom-Json
    if ($metadata.plugin.version -ne $supportedVersion) {
        throw "Unsupported Yuanbao plugin version $($metadata.plugin.version); expected $supportedVersion."
    }
    return $metadata.plugin.rootDir
}

$ProjectDir = [IO.Path]::GetFullPath($ProjectDir)
$mappingFile = Join-Path $ProjectDir "yuanbao_members.json"
$backupRoot = Join-Path $ProjectDir "patch-backups\yuanbao-native-at-$supportedVersion"
$resolvedPluginRoot = Resolve-PluginRoot

foreach ($relative in $relativeFiles) {
    $target = Join-Path $resolvedPluginRoot $relative
    if (-not (Test-Path -LiteralPath $target)) {
        throw "Required plugin file not found: $target"
    }
}

if ($Restore) {
    if (-not (Test-Path -LiteralPath $backupRoot)) {
        throw "Backup not found: $backupRoot"
    }
    foreach ($relative in $relativeFiles) {
        $source = Join-Path $backupRoot $relative
        $target = Join-Path $resolvedPluginRoot $relative
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Backup file missing: $source"
        }
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
    Write-Host "Restored Yuanbao plugin files from $backupRoot"
}
else {
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    foreach ($relative in $relativeFiles) {
        $source = Join-Path $resolvedPluginRoot $relative
        $backup = Join-Path $backupRoot $relative
        if (-not (Test-Path -LiteralPath $backup)) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $source -Destination $backup
        }
    }

    if (-not (Test-Path -LiteralPath $mappingFile)) {
        New-Item -ItemType Directory -Path $ProjectDir -Force | Out-Null
        Write-Utf8 $mappingFile "{`n}`n"
    }

    $mappingLiteral = ConvertTo-Json $mappingFile -Compress

    $handlersPath = Join-Path $resolvedPluginRoot "dist\src\business\messaging\handlers\index.js"
    $handlers = Read-Utf8 $handlersPath
    if (-not $handlers.Contains($patchMarker)) {
        if (-not $handlers.Contains('import fs from "node:fs";')) {
            $handlers = Replace-Required $handlers `
                'import { videoHandler } from "./video.js";' `
                "import { videoHandler } from `"./video.js`";`nimport fs from `"node:fs`"; // $patchMarker" `
                "handlers fs import"
        }
        $start = $handlers.IndexOf('const AT_USER_RE = ')
        $end = $handlers.IndexOf('export function prepareOutboundContent', $start)
        if ($start -lt 0 -or $end -lt 0) {
            throw "Patch point not found: mention resolver."
        }
        $resolver = @"
const AT_USER_RE = /(?<=\s|^)@(\S+?)(?=\s|`$)/g; // $patchMarker
const MEMBER_MAPPING_FILE = $mappingLiteral;
function normalizeMemberName(value) {
    return value.normalize("NFKC").replace(/[\u200B-\u200D\uFEFF]/g, "").trim();
}
function lookupConfiguredMember(groupCode, nickName) {
    try {
        const mappings = JSON.parse(fs.readFileSync(MEMBER_MAPPING_FILE, "utf8"));
        const target = normalizeMemberName(nickName);
        const entry = Object.entries(mappings?.[groupCode] ?? {})
            .find(([name]) => normalizeMemberName(name) === target);
        return entry?.[1] ? { nickName: entry[0], userId: entry[1] } : undefined;
    }
    catch {
        return undefined;
    }
}
function resolveAtMentions(text, groupCode, memberInst) {
    const items = [];
    let lastIndex = 0;
    for (const match of text.matchAll(AT_USER_RE)) {
        const matchStart = match.index ?? 0;
        const nickName = match[1];
        const cached = groupCode && memberInst
            ? memberInst.lookupUserByNickName(groupCode, nickName)
            : undefined;
        const userRecord = cached ?? (groupCode ? lookupConfiguredMember(groupCode, nickName) : undefined);
        if (!userRecord) {
            continue;
        }
        if (matchStart > lastIndex) {
            const before = text.slice(lastIndex, matchStart);
            if (before.trim()) items.push({ type: "text", text: before.trim() });
        }
        items.push({
            type: "custom",
            data: JSON.stringify({
                elem_type: 1002,
                text: ``@`$`{userRecord.nickName`}``,
                user_id: userRecord.userId,
                content: "",
            }),
        });
        lastIndex = matchStart + match[0].length;
    }
    if (lastIndex < text.length) {
        const trailing = text.slice(lastIndex);
        if (trailing.trim()) items.push({ type: "text", text: trailing.trim() });
    }
    if (items.length === 0 && text.trim()) items.push({ type: "text", text: text.trim() });
    return items;
}
"@
        $handlers = $handlers.Substring(0, $start) + $resolver + $handlers.Substring($end)
        Write-Utf8 $handlersPath $handlers
    }

    $extractPath = Join-Path $resolvedPluginRoot "dist\src\business\pipeline\middlewares\extract-content.js"
    $extract = Read-Utf8 $extractPath
    if (-not $extract.Contains($patchMarker) -and -not $extract.Contains('function rememberMembers')) {
        $extract = Replace-Required $extract `
            'import { extractTextFromMsgBody } from "../../messaging/extract.js";' `
            "import { extractTextFromMsgBody } from `"../../messaging/extract.js`";`nimport fs from `"node:fs`"; // $patchMarker`nconst MEMBER_MAPPING_FILE = $mappingLiteral;`nfunction rememberMembers(groupCode, records) {`n    if (!groupCode || groupCode === `"unknown`") return;`n    try {`n        const mappings = fs.existsSync(MEMBER_MAPPING_FILE) ? JSON.parse(fs.readFileSync(MEMBER_MAPPING_FILE, `"utf8`")) : {};`n        const group = mappings[groupCode] ?? {};`n        let changed = false;`n        for (const record of records) {`n            const name = record.name?.replace(/^@/, `"`").trim();`n            const userId = record.userId?.trim();`n            if (name && userId && group[name] !== userId) { group[name] = userId; changed = true; }`n        }`n        if (changed) { mappings[groupCode] = group; fs.writeFileSync(MEMBER_MAPPING_FILE, JSON.stringify(mappings, null, 2) + `"\n`", `"utf8`"); }`n    } catch {}`n}" `
            "automatic member learning helper"
        $needle = '        const { rawBody, isAtBot, medias, mentions, linkUrls } = extractTextFromMsgBody(minCtx, raw.msg_body);'
        $replacement = $needle + "`n        if (isGroup) {`n            rememberMembers(ctx.groupCode, [`n                { name: ctx.senderNickname, userId: ctx.fromAccount },`n                ...(mentions ?? []).map(mention => ({ name: mention.text, userId: mention.userId })),`n            ]);`n        }"
        $extract = Replace-Required $extract $needle $replacement "automatic member learning call"
        Write-Utf8 $extractPath $extract
    }

    $sendPath = Join-Path $resolvedPluginRoot "dist\src\business\actions\text\send.js"
    $send = Read-Utf8 $sendPath
    if (-not $send.Contains($patchMarker) -and -not $send.Contains('function extractAtUserList')) {
        $helper = @"
function extractAtUserList(msgBody) { // $patchMarker
    const users = [];
    for (const elem of msgBody) {
        if (elem?.msg_type !== "TIMCustomElem") continue;
        try {
            const data = JSON.parse(elem.msg_content?.data ?? "{}");
            if (data?.elem_type === 1002 && data?.user_id && !users.includes(data.user_id)) users.push(data.user_id);
        } catch {}
    }
    return users;
}
function requestedAtNames(text) { // $safetyMarker
    return [...new Set([...text.matchAll(/(?<=\s|^)@(\S+?)(?=\s|`$)/g)].map(match => match[1]))];
}
"@
        $send = Replace-Required $send `
            'import { deliver } from "../deliver.js";' `
            ('import { deliver } from "../deliver.js";' + "`n" + $helper) `
            "at user list extractor"
        $send = Replace-Required $send `
            '    return deliver(dt, msgBody);' `
            "    const atUserList = extractAtUserList(msgBody);`n    const requestedNames = isGroup ? requestedAtNames(text) : [];`n    if (requestedNames.length > atUserList.length) {`n        throw new Error(``Native @ resolution failed: requested `${requestedNames.length}, resolved `${atUserList.length}``);`n    }`n    return deliver(dt, msgBody, atUserList);" `
            "send at user list"
    }
    if (-not $send.Contains($safetyMarker)) {
        $send = Replace-Required $send `
            "    return users;`n}`n" `
            "    return users;`n}`nfunction requestedAtNames(text) { // $safetyMarker`n    return [...new Set([...text.matchAll(/(?<=\s|^)@(\S+?)(?=\s|`$)/g)].map(match => match[1]))];`n}`n" `
            "native at safety helper"
        $send = Replace-Required $send `
            "    const atUserList = extractAtUserList(msgBody);`n    return deliver(dt, msgBody, atUserList);" `
            "    const atUserList = extractAtUserList(msgBody);`n    const requestedNames = isGroup ? requestedAtNames(text) : [];`n    if (requestedNames.length > atUserList.length) {`n        throw new Error(``Native @ resolution failed: requested `${requestedNames.length}, resolved `${atUserList.length}``);`n    }`n    return deliver(dt, msgBody, atUserList);" `
            "native at safety validation"
    }
    if (-not $send.Contains($discoveryMarker)) {
        $discoveryHelper = @"
import fs from "node:fs"; // $discoveryMarker
const MEMBER_DISCOVERY_FILE = $mappingLiteral;
function persistDiscoveredMembers(groupCode, records) {
    if (!groupCode || !records?.length) return;
    try {
        const mappings = fs.existsSync(MEMBER_DISCOVERY_FILE) ? JSON.parse(fs.readFileSync(MEMBER_DISCOVERY_FILE, "utf8")) : {};
        const group = mappings[groupCode] ?? {};
        let changed = false;
        for (const record of records) {
            const name = record.nickName?.trim();
            const userId = record.userId?.trim();
            if (name && name !== "unknown" && userId && group[name] !== userId) {
                group[name] = userId;
                changed = true;
            }
        }
        if (changed) {
            mappings[groupCode] = group;
            fs.writeFileSync(MEMBER_DISCOVERY_FILE, JSON.stringify(mappings, null, 2) + "\n", "utf8");
        }
    } catch {}
}
"@
        $send = Replace-Required $send `
            'import { getMember } from "../../../infra/cache/member.js";' `
            ('import { getMember } from "../../../infra/cache/member.js";' + "`n" + $discoveryHelper.TrimEnd()) `
            "member discovery helper"
        $memberNeedle = '    const memberInst = isGroup ? getMember(account.accountId) : undefined;'
        $memberReplacement = $memberNeedle + "`n    if (isGroup && memberInst) {`n        const discovered = await memberInst.queryMembers(groupCode);`n        persistDiscoveredMembers(groupCode, discovered);`n    }"
        $send = Replace-Required $send $memberNeedle $memberReplacement "member discovery call"
    }
    Write-Utf8 $sendPath $send

    $deliverPath = Join-Path $resolvedPluginRoot "dist\src\business\actions\deliver.js"
    $deliver = Read-Utf8 $deliverPath
    if (-not $deliver.Contains($patchMarker) -and -not $deliver.Contains('export async function deliver(dt, msgBody, atUserList = [])')) {
        $deliver = Replace-Required $deliver `
            'export async function deliver(dt, msgBody) {' `
            "export async function deliver(dt, msgBody, atUserList = []) { // $patchMarker" `
            "deliver signature"
        $deliver = Replace-Required $deliver `
            '            traceContext: dt.traceContext,' `
            "            traceContext: dt.traceContext,`n            atUserList," `
            "group delivery at list"
        Write-Utf8 $deliverPath $deliver
    }

    $transportPath = Join-Path $resolvedPluginRoot "dist\src\infra\transport.js"
    $transport = Read-Utf8 $transportPath
    if (-not $transport.Contains($patchMarker) -and -not $transport.Contains('at_user_list: atUserList')) {
        $transport = Replace-Required $transport `
            '    const { account, groupCode, msgBody, fromAccount, refMsgId, refFromAccount, wsClient, traceContext, } = params;' `
            "    const { account, groupCode, msgBody, fromAccount, refMsgId, refFromAccount, wsClient, traceContext, atUserList = [], } = params; // $patchMarker" `
            "transport at list parameter"
        $transport = Replace-Required $transport `
            "            ...(attachRef ? { ref_msg_id: refMsgId } : {}),`n            ...(traceContext ? { trace_id: traceContext.traceId } : {}),`n            ...(traceContext ? { msg_seq: traceContext.nextMsgSeq() } : {})," `
            "            ...(attachRef ? { ref_msg_id: refMsgId } : {}),`n            ...(traceContext ? { trace_id: traceContext.traceId } : {}),`n            ...(traceContext ? { msg_seq: traceContext.nextMsgSeq() } : {}),`n            ...(atUserList.length ? { at_user_list: atUserList } : {})," `
            "transport protobuf at list"
        Write-Utf8 $transportPath $transport
    }

    $codecPath = Join-Path $resolvedPluginRoot "dist\src\access\ws\biz-codec.js"
    $codec = Read-Utf8 $codecPath
    if (-not $codec.Contains($patchMarker) -and -not $codec.Contains('atUserList: data.at_user_list')) {
        $codec = Replace-Required $codec `
            "        refMsgId: data.ref_msg_id ?? `"`",`n        ...(data.msg_seq !== undefined ? { msgSeq: data.msg_seq } : {}),`n        ...(logExt ? { logExt } : {})," `
            "        refMsgId: data.ref_msg_id ?? `"`",`n        ...(data.msg_seq !== undefined ? { msgSeq: data.msg_seq } : {}),`n        ...(logExt ? { logExt } : {}),`n        ...(data.at_user_list?.length ? { atUserList: data.at_user_list } : {}), // $patchMarker" `
            "protobuf codec at list"
        Write-Utf8 $codecPath $codec
    }

    $protoPath = Join-Path $resolvedPluginRoot "dist\src\access\ws\proto\biz.json"
    $proto = Read-Utf8 $protoPath | ConvertFrom-Json
    $fields = $proto.nested.trpc.nested.yuanbao.nested.yuanbao_conn.nested.yuanbao_openclaw_proxy.nested.SendGroupMessageReq.fields
    if (-not $fields.atUserList) {
        $fields | Add-Member -NotePropertyName atUserList -NotePropertyValue ([pscustomobject]@{ rule = "repeated"; type = "string"; id = 10 })
        Write-Utf8 $protoPath (($proto | ConvertTo-Json -Depth 100) + "`n")
    }

    Write-Host "Applied Yuanbao native @ patch to $resolvedPluginRoot"
    Write-Host "Member mapping: $mappingFile"
    Write-Host "Backup: $backupRoot"
}

$nodePath = $null
if (Test-Path -LiteralPath "D:\Node\node.exe") {
    $nodePath = "D:\Node\node.exe"
}
else {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) { $nodePath = $node.Source }
}
if ($nodePath) {
    foreach ($relative in $relativeFiles | Where-Object { $_ -like "*.js" }) {
        & $nodePath --check (Join-Path $resolvedPluginRoot $relative)
        if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed: $relative" }
    }
}
else {
    Write-Warning "node was not found in PATH; JavaScript syntax check skipped."
}

if (-not $SkipRestart -and -not $PluginRoot) {
    Start-Process -FilePath $OpenClawCmd -ArgumentList @("gateway", "run", "--force") -WindowStyle Hidden
    Start-Sleep -Seconds 5
    & $OpenClawCmd gateway status
}

Write-Host "Done. Run this script again after reinstalling or updating the Yuanbao plugin."
