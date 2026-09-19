-- Paginated native widgets: at most one screen of catalog rows is allocated.
local BB = require("ffi/blitbuffer")
local Button = require("ui/widget/button")
local ButtonTable = require("ui/widget/buttontable")
local CenterContainer = require("ui/widget/container/centercontainer")
local Device = require("device")
local Font = require("ui/font")
local FocusManager = require("ui/widget/focusmanager")
local FrameContainer = require("ui/widget/container/framecontainer")
local Geom = require("ui/geometry")
local GestureRange = require("ui/gesturerange")
local HorizontalGroup = require("ui/widget/horizontalgroup")
local HorizontalSpan = require("ui/widget/horizontalspan")
local LineWidget = require("ui/widget/linewidget")
local TextWidget = require("ui/widget/textwidget")
local TitleBar = require("ui/widget/titlebar")
local UIManager = require("ui/uimanager")
local VerticalGroup = require("ui/widget/verticalgroup")
local VerticalSpan = require("ui/widget/verticalspan")
local FocusNav = require("weread.ui.focus_nav")
local PluginUtil = require("weread.lib.plugin_util")
local _ = PluginUtil.tr
local T = PluginUtil.T
local Screen = Device.screen
local Picker = FocusManager:extend{ page = 1 }

function Picker:button(text, width, callback, options)
    local args = { text = text, width = width, callback = callback,
        height = self.row_height, bordersize = 0, padding = 0, margin = 0,
        text_font_size = 20, text_font_bold = false,
        avoid_text_truncation = false, show_parent = self }
    for key, value in pairs(options or {}) do args[key] = value end
    return Button:new(args)
end

function Picker:line()
    return LineWidget:new{
        dimen = Geom:new{ w = self.width, h = self.line_height }, background = BB.COLOR_LIGHT_GRAY,
    }
end

function Picker:actionBar()
    local bar = ButtonTable:new{
        width = self.width, zero_sep = true, show_parent = self,
        buttons = { {
            { text = T(_("Get thoughts (%1 chapters)"), self.model.count), enabled = self.model.count > 0,
                callback = function()
                    local chapters = self.model:selection()
                    if #chapters == 0 then return end
                    self:onClose()
                    self.on_select(chapters)
                end },
        } },
    }
    -- The outer focus manager owns all rows, including the native action bar.
    bar.key_events, bar.layout = {}, nil
    return bar
end

function Picker:init()
    self.width, self.height = Screen:getWidth(), Screen:getHeight()
    self.row_height, self.line_height = Screen:scaleBySize(88), math.max(1, Screen:scaleBySize(1))
    self.page_bar_height = Screen:scaleBySize(48)
    self.dimen = Geom:new{ x = 0, y = 0, w = self.width, h = self.height }
    self.covers_fullscreen = true
    self.title_bar = TitleBar:new{
        width = self.width, title = _("Choose chapters to match"),
        subtitle = self.book_title, title_face = Font:getFace("tfont", 24),
        with_bottom_line = true, show_parent = self,
        close_callback = function() self:onClose() end,
    }
    self.hint = FrameContainer:new{
        padding = Screen:scaleBySize(10), margin = 0, bordersize = 0,
        TextWidget:new{ text = _("Select chapters. Fetching again replaces their saved thoughts."),
            face = Font:getFace("cfont", 14), fgcolor = BB.COLOR_DARK_GRAY,
            max_width = self.width - Screen:scaleBySize(20) },
    }
    self.actions = self:actionBar()
    local available = self.height - self.title_bar:getHeight() - self.hint:getSize().h
        - self.page_bar_height - self.line_height - self.actions:getSize().h
    self.per_page = math.max(1, math.floor(available / (self.row_height + self.line_height)))
    self.list_height = available
    for index, node in ipairs(self.model:visible()) do
        if node == self.model.current then self.page = math.ceil(index / self.per_page); break end
    end
    if Device:hasKeys() then
        self.key_events.Close = { { Device.input.group.Back } }
        self.key_events.NextPage = { { Device.input.group.PgFwd } }
        self.key_events.PrevPage = { { Device.input.group.PgBack } }
    end
    self.ges_events.Swipe = { GestureRange:new{ ges = "swipe", range = self.dimen } }
    self:rebuild()
end

function Picker:edit(node)
    if not self.on_edit or not node.xpointer then return end
    self.on_edit(node, function(model)
        local selected = {}
        for _, old in ipairs(self.model.nodes) do
            if old.selected then selected[old.toc_index or old.index] = true end
        end
        for _, new in ipairs(model.nodes) do
            if selected[new.toc_index or new.index] then model:toggle(new) end
        end
        self.model = model
        self:rebuild(model.nodes[node.index], 3)
    end)
end

function Picker:rebuild(focus_node, focus_column)
    -- The title and hint are reused; free all previous page widgets before
    -- allocating new ones. No hidden chapter owns a font/gesture/widget tree.
    if self.body then
        self.body:free()
        self.actions = self:actionBar()
    end
    local visible = self.model:visible()
    self.pages = math.max(1, math.ceil(#visible / self.per_page))
    self.page = math.max(1, math.min(self.page, self.pages))
    local list, focus_rows = VerticalGroup:new{ align = "left" }, {}
    local side, status_width = Screen:scaleBySize(44), Screen:scaleBySize(92)
    local half = math.floor(self.row_height / 2)
    local focus_y = 1
    for index = (self.page - 1) * self.per_page + 1, math.min(#visible, self.page * self.per_page) do
        local node = visible[index]
        local indent = math.min(node.depth, 5) * Screen:scaleBySize(14)
        local toggle = function() self.model:toggle(node); self:rebuild(node, 2) end
        local content_width = self.width - side - indent - status_width
        local title = self:button(node.title, content_width, toggle, {
            height = half, align = "left", text_font_bold = node.branch or node == self.model.current,
            enabled = node.selectable })
        local remote = self:button(node.chapter and T(_("WeRead: %1"), node.chapter.title or "")
            or _("Choose a WeRead chapter first"), content_width, toggle, {
            height = self.row_height - half, align = "left", text_font_size = 14,
            enabled = node.selectable })
        local check = self:button(node.selected and "✓" or "□", side, toggle,
            { text_font_size = 24, enabled = node.selectable })
        local status = node.chapter and (node.fetched and _("Retrieved") or _("Matched")) or _("Unmatched")
        local label = CenterContainer:new{ dimen = Geom:new{ w = status_width, h = half },
            FrameContainer:new{ bordersize = 0, margin = 0, padding = Screen:scaleBySize(3),
                background = node.chapter and BB.COLOR_BLACK or BB.COLOR_WHITE,
                TextWidget:new{ text = status, face = Font:getFace("cfont", 13),
                    max_width = status_width - Screen:scaleBySize(6),
                    fgcolor = node.chapter and BB.COLOR_WHITE or BB.COLOR_DARK_GRAY } } }
        local edit = self:button(node.chapter and _("Change match") or _("Select match"), status_width,
            function() self:edit(node) end, { height = self.row_height - half, text_font_size = 16,
                enabled = self.on_edit ~= nil and node.xpointer ~= nil })
        local row = HorizontalGroup:new{ align = "center", check, HorizontalSpan:new{ width = indent },
            VerticalGroup:new{ align = "left", title, remote },
            VerticalGroup:new{ align = "center", label, edit } }
        list[#list + 1], list[#list + 2] = row, self:line()
        focus_rows[#focus_rows + 1] = { check, title, edit }
        if node == focus_node then focus_y = #focus_rows end
    end
    list[#list + 1] = VerticalSpan:new{ width = math.max(0, self.list_height - list:getSize().h) }
    -- getSize() caches offsets for the rows above. The appended spacer needs
    -- its own offset before the first paint (and after every page rebuild).
    list:resetLayout()
    local third = math.floor(self.width / 3)
    local previous = self:button("‹", third, function() self:onPrevPage() end,
        { enabled = self.page > 1, height = self.page_bar_height })
    local counter = self:button(tostring(self.page) .. " / " .. tostring(self.pages), third, nil,
        { enabled = false, height = self.page_bar_height })
    local next_page = self:button("›", self.width - third * 2, function() self:onNextPage() end,
        { enabled = self.page < self.pages, height = self.page_bar_height })
    focus_rows[#focus_rows + 1] = { previous, counter, next_page }
    focus_rows[#focus_rows + 1] = self.actions.buttons_layout[1]
    self.layout = focus_rows
    self.body = VerticalGroup:new{ align = "left", list, self:line(),
        HorizontalGroup:new{ previous, counter, next_page }, self.actions }
    self[1] = FrameContainer:new{
        background = BB.COLOR_WHITE, bordersize = 0, padding = 0, margin = 0,
        VerticalGroup:new{ align = "left", self.title_bar, self.hint, self.body },
    }
    FocusNav.initialFocus(self, focus_column or 2, focus_y)
    UIManager:setDirty(self, "ui")
end

function Picker:onNextPage()
    if self.page < self.pages then self.page = self.page + 1; self:rebuild() end
    return true
end

function Picker:onPrevPage()
    if self.page > 1 then self.page = self.page - 1; self:rebuild() end
    return true
end

function Picker:onSwipe(_, ges)
    if ges.direction == "west" then return self:onNextPage() end
    if ges.direction == "east" then return self:onPrevPage() end
    return true
end

function Picker:onShow()
    UIManager:setDirty(self, "ui")
    return true
end

function Picker:onClose()
    UIManager:close(self)
    return true
end

function Picker:onCloseWidget()
    UIManager:setDirty(nil, "ui")
end

local M = {}
function M.show(options)
    local picker = Picker:new(options)
    UIManager:show(picker)
    return picker
end
return M
