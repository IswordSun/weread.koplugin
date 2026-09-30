-- KOReader creates one plugin instance per document, and LuaSettings:flush()
-- writes the whole in-memory table. If every instance kept its own store, an
-- instance whose document was closed long ago would flush its stale snapshot and
-- silently roll back newer writes (settings, the books index, cookies, tokens).
-- This spec models the real whole-table file semantics and pins the sharing.

package.path = "./?.lua;./?/init.lua;" .. package.path

local checks = 0
local function expect(condition, message)
    checks = checks + 1
    if not condition then error(message or ("check " .. checks .. " failed")) end
end

local function deepcopy(value)
    if type(value) ~= "table" then return value end
    local out = {}
    for key, item in pairs(value) do out[key] = deepcopy(item) end
    return out
end

-- path -> persisted table, exactly what a real LuaSettings file holds.
local files = {}
local opens_per_path = {}

package.preload["datastorage"] = function()
    return {
        getFullDataDir = function() return "/data" end,
        getSettingsDir = function() return "/settings" end,
    }
end
package.preload["luasettings"] = function()
    return { open = function(_self, path)
        opens_per_path[path] = (opens_per_path[path] or 0) + 1
        -- Reading the file copies it, and flushing writes the whole copy back:
        -- the two properties that make a stale instance dangerous.
        local data = deepcopy(files[path] or {})
        return {
            readSetting = function(_store, key, default)
                if data[key] == nil then return default end
                return data[key]
            end,
            saveSetting = function(_store, key, value) data[key] = value end,
            delSetting = function(_store, key) data[key] = nil end,
            flush = function() files[path] = deepcopy(data) end,
        }
    end }
end
local directories = {}
package.preload["libs/libkoreader-lfs"] = function()
    return {
        attributes = function(path) return directories[path] and "directory" end,
        mkdir = function(path) directories[path] = true; return true end,
    }
end
package.preload["weread.lib.book_store"] = function()
    return {
        load = function(_settings, _id, index) return index end,
        save = function(_settings, _id, book) return true, book end,
    }
end

local Settings = require("weread.lib.settings")
-- Kept optional so the assertions below fail meaningfully on a revision without sharing.
local reset_shared = Settings._reset_shared_stores or function() end
local production_path = "/settings/weread.lua"

-- The file manager instance reads the settings when the app starts. Opening the
-- file seeds defaults (cache flags), so the file exists from here on.
local stale = Settings:new()
expect(type(files[production_path]) == "table", "opening settings did not seed defaults")

-- ... the reader instance is created for the book, and both write and flush.
local fresh = Settings:new()
fresh:set("api_key", "fresh-write")
fresh:flush()
stale:set("shelf", { paginated = false })
stale:flush()

expect(files[production_path].api_key == "fresh-write",
    "a stale instance rolled back a newer write")
expect(files[production_path].shelf and files[production_path].shelf.paginated == false,
    "the stale instance's own write was lost")

-- A later instance sees everything, and the two façades share one store.
local third = Settings:new()
expect(third:get("api_key") == "fresh-write", "a later instance did not see the newer write")
expect(third:get("shelf").paginated == false, "a later instance did not see the other write")
expect(fresh.store == stale.store and third.store == stale.store,
    "instances of the same settings file must share one store")
expect(opens_per_path[production_path] == 1,
    "the settings file was opened once per instance")

-- Distinct settings files (production vs mock) must stay independent.
files["/settings/weread-environment.lua"] = { enabled = true, host = "192.168.31.111", port = 8765 }
package.loaded["weread.lib.mock_environment"] = nil
reset_shared()
local mock = Settings:new()
expect(mock.settings_file == "/settings/weread-mock.lua", "the mock settings file was not selected")
expect(mock.store ~= stale.store, "mock and production stores must not be shared")
expect(mock:get("account").login_method == "mock", "the mock account was not seeded")
mock:set("shelf", { paginated = true })
mock:flush()
expect(files[production_path].shelf.paginated == false,
    "a mock write leaked into the production settings")
expect(files["/settings/weread-mock.lua"].shelf.paginated == true,
    "the mock settings were not persisted")

-- A fresh process (no shared stores) reads what was flushed last.
files["/settings/weread-environment.lua"] = { enabled = false, host = "192.168.31.111", port = 8765 }
package.loaded["weread.lib.mock_environment"] = nil
reset_shared()
local reopened = Settings:new()
expect(reopened.settings_file == production_path, "the production file was not reselected")
expect(reopened.store ~= mock.store, "resetting must drop the shared stores")
expect(reopened:get("api_key") == "fresh-write",
    "a fresh process did not read the flushed production settings")

print("settings_sharing_spec: " .. checks .. " checks passed")
