-- Project user-defined WeRead shelf groups onto the current shelf snapshot.

local Groups = {}

local function text(value, fallback)
    if type(value) ~= "string" then return fallback end
    value = value:match("^%s*(.-)%s*$")
    return value ~= "" and value or fallback
end

function Groups.list(archives, books, unnamed_label)
    local by_id = {}
    for _, book in ipairs(type(books) == "table" and books or {}) do
        local id = book.book_id or book.bookId
        if id then by_id[tostring(id)] = book end
    end

    local groups = {}
    for index, archive in ipairs(type(archives) == "table" and archives or {}) do
        local members, seen = {}, {}
        for _, id in ipairs(type(archive.bookIds) == "table" and archive.bookIds or {}) do
            id = tostring(id)
            local book = by_id[id]
            if book and not seen[id] then
                members[#members + 1] = book
                seen[id] = true
            end
        end
        groups[#groups + 1] = {
            key = index,
            label = text(archive.name, unnamed_label or "Unnamed group"),
            books = members,
        }
    end
    return groups
end

function Groups.find(groups, key)
    for _, group in ipairs(type(groups) == "table" and groups or {}) do
        if group.key == key then return group end
    end
end

return Groups
