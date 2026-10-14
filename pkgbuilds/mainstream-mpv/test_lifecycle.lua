local events, messages, callbacks, cancelled, props = {}, {}, {}, {}, {}
local counter, commands = 0, {}
props.path = ''
local mp = {
    get_property = function(key, default) return props[key] or default end,
    set_property_native = function(key, value) props[key] = value end,
    abort_async_command = function(id) cancelled[id] = true end,
    register_event = function(name, fn) events[name] = fn end,
    register_script_message = function(name, fn) messages[name] = fn end,
    osd_message = function() end,
    msg = {warn = function() end},
    command_native_async = function(args, callback)
        counter = counter + 1; callbacks[counter] = callback; commands[counter] = args; return counter
    end,
}
package.preload['mp'] = function() return mp end
package.preload['mp.utils'] = function() return {
    split_path = function(path) return '', path:match('[^/]+$') end,
    parse_json = function(value) return value end,
} end
package.preload['mp.options'] = function() return {read_options = function() end} end
dofile(arg[1])
local function complete(id, title)
    callbacks[id](true, {status = 0, stdout = {displayTitle = title, sourceUrl = 'https://www.tvmaze.com/episodes/1'}}, nil)
end
props.path = '/videos/Show.S01E01.mkv'; events['file-loaded'](); assert(counter == 1)
props.path = '/videos/Show.S01E02.mkv'; events['start-file'](); events['file-loaded'](); assert(counter == 2 and cancelled[1])
complete(1, 'Old episode'); assert(props['user-data/mainstream-metadata'].displayTitle == nil)
complete(2, 'Current episode'); assert(props['user-data/mainstream-metadata'].displayTitle == 'Current episode')
assert(props['user-data/mainstream-metadata'].path == props.path)
events['end-file'](); assert(props['user-data/mainstream-metadata'].displayTitle == nil)
props.path = 'https://example.com/Show.S01E01.mkv'; events['file-loaded'](); assert(counter == 2)
props.path = '/videos/Home video.mp4'; events['file-loaded'](); assert(counter == 2)
props.path = '/videos/Show.S01E03.mkv'; events['file-loaded'](); assert(counter == 3)
events.shutdown(); assert(cancelled[3])
props.path = 'file:///videos/The%20Show.S01E01.mkv'; events['file-loaded'](); assert(counter == 4)
assert(commands[4].args[4] == 'The Show.S01E01.mkv')
complete(4, 'Local URI'); assert(props['user-data/mainstream-metadata'].path == props.path)
print('Lua lifecycle checks passed: stale lookups, cancellation, cleanup, remote URLs, and unrecognized files.')
