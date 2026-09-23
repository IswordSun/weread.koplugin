package.path = "./?.lua;./?/init.lua;" .. package.path

local checks = 0
local function expect(condition, message)
    checks = checks + 1
    if not condition then error(message or ("check " .. checks .. " failed")) end
end

local Crypto = require("weread.lib.crypto")
local DeviceIdentity = require("weread.lib.device_identity")

local function fake_settings(seed)
    return {
        values = { device_seed = seed },
        set_calls = {},
        flush_calls = 0,
        get = function(self, key, default)
            local value = self.values[key]
            if value == nil then return default end
            return value
        end,
        set = function(self, key, value)
            self.values[key] = value
            self.set_calls[#self.set_calls + 1] = key
        end,
        flush = function(self)
            self.flush_calls = self.flush_calls + 1
        end,
    }
end

local settings = fake_settings("fixed-seed")
local identity = DeviceIdentity.ensure(settings)
expect(identity.fp == Crypto.sha256_hex("fixed-seed"),
    "fp must be the SHA-256 of the persistent seed")
expect(#identity.fp == 64 and identity.fp:match("^%x+$") ~= nil,
    "fp must be 64 lowercase hex characters")
expect(identity.device_id:match("^eink%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d$") ~= nil,
    "device_id must be eink plus 19 decimal digits")
local derived = tonumber(identity.fp:sub(1, 12), 16)
expect(identity.device_id == "eink" .. string.format("%019d", derived),
    "device_id must derive from the first 12 hex digits of fp")
expect(settings.flush_calls == 0,
    "an existing seed must not be rewritten or flushed")

local again = DeviceIdentity.ensure(settings)
expect(again.fp == identity.fp and again.device_id == identity.device_id,
    "identity must be stable across calls for one settings store")

local other = DeviceIdentity.ensure(fake_settings("other-seed"))
expect(other.fp ~= identity.fp and other.device_id ~= identity.device_id,
    "different seeds must produce different identities")

local fresh = fake_settings("")
local generated = DeviceIdentity.ensure(fresh)
expect(type(fresh.values.device_seed) == "string" and #fresh.values.device_seed == 32,
    "a missing seed must be generated and persisted as 32 hex characters")
expect(fresh.set_calls[1] == "device_seed",
    "the generated seed must be stored under device_seed")
expect(fresh.flush_calls == 1,
    "the generated seed must be flushed once so it survives restarts")
local regenerated = DeviceIdentity.ensure(fresh)
expect(regenerated.fp == generated.fp and fresh.flush_calls == 1,
    "a persisted seed must not be regenerated on later calls")

local fresh_second = fake_settings("")
local generated_second = DeviceIdentity.ensure(fresh_second)
expect(generated.fp ~= generated_second.fp,
    "independently generated identities must differ")

print(("device_identity_spec: %d checks"):format(checks))
