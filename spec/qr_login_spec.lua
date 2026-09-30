package.path = "./?.lua;./?/init.lua;" .. package.path

local checks = 0
local function expect(condition, message)
    checks = checks + 1
    if not condition then error(message or ("check " .. checks .. " failed")) end
end

package.preload["device"] = function()
    return { screen = { getWidth = function() return 600 end,
        getHeight = function() return 800 end } }
end
package.preload["weread.lib.i18n"] = function()
    return { tr = function(text) return text end }
end
package.preload["ui/widget/inputdialog"] = function() return {} end
package.preload["weread.lib.logger"] = function()
    return {
        scoped = function()
            return { warn = function() end, err = function() end }
        end,
    }
end
package.preload["ui/widget/qrmessage"] = function() return {} end
package.preload["ffi/util"] = function()
    return { template = function(text) return text end }
end
package.preload["ui/uimanager"] = function() return {} end
package.preload["weread.lib.protocol"] = function()
    return {
        urlencode = function(value)
            return tostring(value):gsub(" ", "%%20")
        end,
    }
end

local requests = {}
local client = {
    request = function(_self, options)
        requests[#requests + 1] = options
        return "{}", 200, {}
    end,
    decode_http_json = function()
        return { logicCode = "PENDING" }
    end,
}

local QRLogin = require("weread.lib.qr_login")
local login = QRLogin:new({}, client, {})

login:_poll_protocol("uid value", "")
expect(requests[1].url ==
    "https://weread.qq.com/api/auth/getLoginInfo?uid=uid%20value&otp=",
    "empty OTP was not serialized with an explicit value")

login:_poll_protocol("uid value", "1234")
expect(requests[2].url ==
    "https://weread.qq.com/api/auth/getLoginInfo?uid=uid%20value&otp=1234",
    "non-empty OTP was serialized incorrectly")

-- A transport failure (no HTTP response at all) is reported by the client as a
-- nil result, not as an exception. Both the login start and the completion chain
-- must turn that into a described error instead of a nil index crash: the crash
-- used to happen after the phone had already confirmed the QR code, forcing the
-- user to scan again.
local failing_client = {
    -- The login page itself succeeds, so the chain reaches the fallback paths
    -- where a missing response used to be indexed as if it were a table.
    request_follow = function() return "<html></html>", 200, {} end,
    post_json = function() return nil, nil, nil, "connection refused" end,
    request = function() return nil, nil, nil, "connection refused" end,
}
local failing_login = QRLogin:new({}, failing_client, {})

local fetch_ok, fetch_err = pcall(function()
    return failing_login:_authenticated_get(
        "https://weread.qq.com/api/userInfo", {}, "vid", "skey", "userInfo")
end)
expect(fetch_ok == false, "a transport failure was reported as success")
expect(tostring(fetch_err):find("no usable response", 1, true) ~= nil,
    "the transport failure was not described: " .. tostring(fetch_err))
expect(tostring(fetch_err):find("attempt to index", 1, true) == nil,
    "a transport failure still crashed with a nil index: " .. tostring(fetch_err))

local begin_ok, begin_err = pcall(function() return failing_login:_begin_protocol() end)
expect(begin_ok == false, "a transport failure during login start was reported as success")
expect(tostring(begin_err):find("valid login UID", 1, true) ~= nil,
    "the login start failure was not described: " .. tostring(begin_err))
expect(tostring(begin_err):find("attempt to index", 1, true) == nil,
    "the classic fallback still crashed with a nil index: " .. tostring(begin_err))

print("qr_login_spec: " .. checks .. " checks passed")
