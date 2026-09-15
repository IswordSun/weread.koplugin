package.path = "./?.lua;./?/init.lua;" .. package.path

local WeRead = require("weread.lib.protocol")

local function expect(actual, expected, message)
    if actual ~= expected then error(message) end
end

expect(
    WeRead.normalize_cover_url("http://wx.qlogo.cn/mmopen/avatar"),
    "https://wx.qlogo.cn/mmopen/avatar",
    "WeChat public-account avatar did not upgrade to HTTPS"
)
expect(
    WeRead.normalize_cover_url("http://example.com/cover"),
    "http://example.com/cover",
    "unrelated HTTP cover URL was unexpectedly changed"
)
expect(
    WeRead.normalize_cover_url("https://weread.qq.com/cover/t3_book"),
    "https://weread.qq.com/cover/t9_book",
    "WeRead cover resolution normalization regressed"
)

print("protocol_cover_url_spec: 3 checks")
