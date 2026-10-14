local mp = require 'mp'
local utils = require 'mp.utils'
local options = require 'mp.options'
local opts = {enabled = true, helper = os.getenv('MAINSTREAM_MPV_METADATA_HELPER') or '/usr/lib/mainstream-mpv/metadata.py', show_credit = true}
options.read_options(opts, 'mainstream-metadata')
local request_id, generation = nil, 0
local property = 'user-data/mainstream-metadata'

local function cancel()
    generation = generation + 1
    if request_id then mp.abort_async_command(request_id); request_id = nil end
    mp.set_property_native(property, {})
end

local function refresh(force)
    cancel()
    if not opts.enabled then return end
    local path = mp.get_property('path', '')
    local file_uri = path:match('^file:///') or path:match('^file://localhost/')
    if path == '' or (path:match('^%a[%w+.-]*://') and not file_uri) then return end
    local _, basename = utils.split_path(path)
    if file_uri then
        basename = basename:gsub('%%(%x%x)', function(hex) return string.char(tonumber(hex, 16)) end)
    end
    if not basename:lower():match('s%d%d?e%d+') then return end
    local token = generation
    local args = {'python3', opts.helper, '--filename', basename}
    if force then args[#args + 1] = '--refresh' end
    request_id = mp.command_native_async({name = 'subprocess', args = args,
        playback_only = false, capture_stdout = true, capture_stderr = true}, function(success, result, error)
        if token ~= generation or mp.get_property('path', '') ~= path then return end
        request_id = nil
        if not success or not result or result.status ~= 0 then
            mp.msg.warn((result and result.stderr) or error or 'Episode lookup failed')
            return
        end
        local data = utils.parse_json(result.stdout or '')
        if type(data) ~= 'table' or type(data.displayTitle) ~= 'string' then return end
        data.path = path
        mp.set_property_native(property, data)
        if opts.show_credit then
            mp.osd_message('Episode information: TVmaze\n' .. (data.sourceUrl or ''), 4)
        end
    end)
end

mp.register_event('start-file', cancel)
mp.register_event('end-file', cancel)
mp.register_event('file-loaded', function() refresh(false) end)
mp.register_event('shutdown', cancel)
mp.register_script_message('metadata-refresh', function() refresh(true) end)
-- Supports loading the script into an already initialized player for tests.
if mp.get_property('path', '') ~= '' then refresh(false) end
