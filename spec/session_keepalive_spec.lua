-- The session keep-alive timer is tied to its plugin instance. KOReader builds a
-- fresh plugin instance for every document, so an instance whose book was closed
-- must stop ticking: otherwise the task keeps the whole instance (and its stale
-- settings snapshot) alive and its next renewal rewrites settings/weread.lua from
-- that snapshot, discarding newer writes from other instances.

package.path = "./?.lua;./?/init.lua;" .. package.path

local checks = 0
local function expect(condition, message)
    checks = checks + 1
    if not condition then error(message or ("check " .. checks .. " failed")) end
end

local scheduled, unscheduled = {}, {}
local next_task_id = 0

package.preload["ui/bidi"] = function() return {} end
package.preload["ui/widget/confirmbox"] = function() return {} end
package.preload["ui/widget/infomessage"] = function() return {} end
package.preload["ui/widget/notification"] = function() return {} end
package.preload["ui/widget/menu"] = function() return {} end
package.preload["weread.lib.logger"] = function()
    return { info = function() end, warn = function() end, err = function() end }
end
package.preload["weread.lib.session_state"] = function()
    return { is_invalid = function() return false end, clear = function() end }
end
package.preload["weread.lib.plugin_util"] = function()
    return {
        tr = function(text) return text end,
        T = function(text) return text end,
        log_error = tostring,
        display_error = tostring,
        unpack_args = function(...) return ... end,
    }
end
package.preload["ui/uimanager"] = function()
    return {
        scheduleIn = function(_self, delay, callback)
            next_task_id = next_task_id + 1
            scheduled[#scheduled + 1] = { id = next_task_id, delay = delay, callback = callback }
            return next_task_id
        end,
        unschedule = function(_self, task_id)
            unscheduled[#unscheduled + 1] = task_id
        end,
        show = function() end,
    }
end

local Common = require("weread.ui.common")

-- The real plugin gets these through the mixin; mirror that here.
local function new_instance()
    local instance = { settings = { get = function() return 0 end } }
    instance.startSessionKeepAliveTimer = Common.startSessionKeepAliveTimer
    instance.stopSessionKeepAliveTimer = Common.stopSessionKeepAliveTimer
    return instance
end

local instance = new_instance()

Common.startSessionKeepAliveTimer(instance)
expect(#scheduled == 1, "the keep-alive timer was not scheduled")
expect(scheduled[1].delay == 15 * 60, "unexpected keep-alive interval")

-- Starting twice on the same instance must not leave two timers behind.
Common.startSessionKeepAliveTimer(instance)
expect(#scheduled == 1, "a second keep-alive timer was scheduled for one instance")

-- A tick re-arms itself so long reading sessions stay covered.
scheduled[1].callback()
expect(#scheduled == 2, "the keep-alive timer did not re-arm after a tick")
expect(instance._session_keepalive_task ~= nil, "the pending task handle was not retained")

Common.stopSessionKeepAliveTimer(instance)
expect(#unscheduled == 1 and unscheduled[1] == 2,
    "closing the document did not unschedule the pending keep-alive task")

-- A tick already queued when the document closes must not re-arm.
scheduled[2].callback()
expect(#scheduled == 2, "a stopped keep-alive timer re-armed itself after the document closed")

-- Two instances (e.g. file manager plus reader) keep independent timers, and
-- stopping one leaves the other alone.
local other = new_instance()
Common.startSessionKeepAliveTimer(other)
expect(#scheduled == 3, "a second instance did not get its own keep-alive timer")
Common.stopSessionKeepAliveTimer(other)
expect(#unscheduled == 2 and unscheduled[2] == 3,
    "stopping one instance cancelled the wrong task")

print("session_keepalive_spec: " .. checks .. " checks passed")
