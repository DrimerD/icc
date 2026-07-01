--[[
  dograh_inbound.lua

  Invoked from the FreeSWITCH dialplan on an inbound call. It:
    1. POSTs call metadata to Dograh's inbound dispatcher
       (/api/v1/telephony/inbound/run);
    2. reads back the WebSocket URL Dograh allocated for this call;
    3. starts mod_audio_fork to stream the call audio to that URL.

  Dograh matches the called number (To) to a workflow, creates a run and
  returns { "websocket_url": "wss://.../ws/<wf>/<user>/<run>" }.

  Auth: we send the shared secret in the X-Dograh-Token header (compared in
  constant time on the Dograh side). Always run this over TLS (https/wss).

  Requirements on the FreeSWITCH host:
    - mod_lua loaded
    - mod_audio_fork (or mod_audio_stream) loaded
    - the `curl` binary available on the host (used for the HTTP POST)
--]]

-- ======== CONFIG — edit these ========
local DOGRAH_BASE    = "https://api.dograh.com"   -- self-hosted: your backend base URL
local ACCOUNT_ID     = "fs-prod-1"                -- must match Account ID in Dograh config
local SHARED_SECRET  = "CHANGE_ME"                -- must match Shared Secret in Dograh config
local SAMPLE_RATE    = "8000"                     -- must match provider transport_sample_rate
local MIX_TYPE       = "mono"                     -- caller audio only
local STREAM_APP     = "audio_fork"               -- or "audio_stream" for mod_audio_stream

-- Custom call variables are collected automatically: every inbound SIP header
-- named X-*/x-*/X_*/x_* is forwarded to Dograh, where the prefix is stripped.
-- So an INVITE header `X-first_name: Ada` becomes {{first_name}} in prompts.
-- Inbound SIP headers are exposed by FreeSWITCH as channel variables named
-- `sip_h_<Header-Name>`; we enumerate them via `uuid_dump`.
-- =====================================

local to_number   = session:getVariable("destination_number") or ""
local from_number  = session:getVariable("caller_id_number") or ""
local call_uuid   = session:getVariable("uuid") or ""

freeswitch.consoleLog("info",
  string.format("[dograh] inbound call uuid=%s to=%s from=%s\n",
    call_uuid, to_number, from_number))

-- Minimal JSON string escaper for header values.
local function esc(s)
  s = tostring(s or "")
  s = s:gsub("\\", "\\\\"):gsub('"', '\\"')
  s = s:gsub("[\r\n\t]", " ")
  return s
end

-- Collect all inbound SIP headers named X-*/x-*/X_*/x_* by dumping the channel
-- and scanning for `variable_sip_h_<name>` lines. We forward the original
-- header name (minus the sip_h_ prefix); Dograh strips the x-/x_ prefix.
local api = freeswitch.API()
local dump = api:executeString("uuid_dump " .. call_uuid) or ""

local var_parts = {}
for line in dump:gmatch("[^\r\n]+") do
  -- match "variable_sip_h_X-first_name: Ada" (and a no-prefix fallback)
  local name, value = line:match("^variable_sip_h_([^:]+):%s*(.*)$")
  if not name then
    name, value = line:match("^sip_h_([^:]+):%s*(.*)$")
  end
  -- keep only X-/x-/X_/x_ headers
  if name and name:match("^[xX][-_]") and value and value ~= "" then
    table.insert(var_parts,
      string.format('"%s":"%s"', esc(name), esc(value)))
  end
end
local variables_json = "{" .. table.concat(var_parts, ",") .. "}"

freeswitch.consoleLog("info",
  "[dograh] forwarding variables: " .. variables_json .. "\n")

-- Build a compact JSON body (no spaces — keeps shell/arg parsing simple).
local body = string.format(
  '{"To":"%s","From":"%s","CallSid":"%s","account_id":"%s","provider":"freeswitch","variables":%s}',
  esc(to_number), esc(from_number), esc(call_uuid), esc(ACCOUNT_ID), variables_json)

-- POST to the Dograh inbound dispatcher using the system `curl` binary.
local url = DOGRAH_BASE .. "/api/v1/telephony/inbound/run"
local cmd = string.format(
  'curl -s -m 10 -X POST %q '
  .. '-H "Content-Type: application/json" '
  .. '-H "X-Dograh-Provider: freeswitch" '
  .. '-H "X-Dograh-Token: %s" '
  .. '-d %q',
  url, SHARED_SECRET, body)

local handle = io.popen(cmd)
local response = handle and handle:read("*a") or ""
if handle then handle:close() end

freeswitch.consoleLog("info", "[dograh] inbound/run response: " .. response .. "\n")

-- Extract websocket_url without a JSON library.
local ws_url = response:match('"websocket_url"%s*:%s*"([^"]+)"')

if not ws_url or ws_url == "" then
  freeswitch.consoleLog("err",
    "[dograh] no websocket_url returned — hanging up\n")
  session:hangup("NORMAL_TEMPORARY_FAILURE")
  return
end

-- Answer and start streaming audio to Dograh.
session:answer()

-- mod_audio_fork app args:  start <wss-url> <mix-type> <sampling-rate> [metadata]
local fork_args = string.format("start %s %s %s", ws_url, MIX_TYPE, SAMPLE_RATE)
freeswitch.consoleLog("info",
  string.format("[dograh] %s %s\n", STREAM_APP, fork_args))
session:execute(STREAM_APP, fork_args)

-- Keep the call up while the media streams; the agent ends the call by
-- hanging up the channel (or the caller hangs up).
session:execute("park")
