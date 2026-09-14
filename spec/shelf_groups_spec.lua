package.path = "./?.lua;./?/init.lua;" .. package.path

local Groups = require("weread.lib.shelf_groups")
local checks = 0
local function expect(condition, message)
    checks = checks + 1
    if not condition then error(message or ("check " .. checks .. " failed")) end
end

local books = {
    { bookId = "one", title = "One" },
    { bookId = "two", title = "Two" },
    { bookId = "three", title = "Three" },
}
local groups = Groups.list({
    { name = "Reading", bookIds = { "two", "one", "two", "missing" } },
    { name = "", bookIds = { "three" } },
}, books, "Unnamed group")

expect(#groups == 2, "user-defined groups were dropped")
expect(groups[1].label == "Reading" and #groups[1].books == 2,
    "group did not retain known members in user order")
expect(groups[1].books[1].bookId == "two" and groups[1].books[2].bookId == "one",
    "group order or duplicate handling is wrong")
expect(groups[2].label == "Unnamed group" and groups[2].books[1].bookId == "three",
    "blank group name did not retain its book")
expect(Groups.find(groups, 1) == groups[1] and Groups.find(groups, 99) == nil,
    "group selection lookup is not stable")

print(("shelf_groups_spec: %d checks"):format(checks))
