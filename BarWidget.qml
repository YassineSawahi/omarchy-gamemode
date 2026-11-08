import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.yassinesawahi.gamemode"

  readonly property string scriptPath: decodeURIComponent(Qt.resolvedUrl("gamemode.py").toString().replace(/^file:\/\//, ""))
  readonly property string padGlyph: String.fromCodePoint(0xF0297) // nf-md-gamepad_variant

  // Latest `gamemode.py status` reading; null until the first one lands.
  property var status: null
  property bool popupOpen: false
  property bool refreshQueued: false

  // "on" or "off" clicked but not yet finished. Switches run detached, so they
  // survive a shell restart; while one holds the lock the bar polls fast.
  property string pending: ""
  // key → ticked, for a tick not yet confirmed by a reading.
  property var selectPending: ({})
  // key → "pause" or "resume", for one item switched while game mode is on.
  property var itemPending: ({})

  readonly property bool working: root.pending !== "" || Object.keys(root.itemPending).length > 0
    || (!!root.status && root.status.busy)
  readonly property bool gameOn: root.pending !== "" ? root.pending === "on" : !!root.status && root.status.on
  readonly property var items: root.status ? root.status.items : []
  readonly property var mem: root.status ? root.status.mem : null
  readonly property var gpu: root.status ? root.status.gpu : null

  function gb(mb) {
    return Commons.I18n.formatNumber(mb / 1024, 1) + " GB"
  }

  function size(mb) {
    return mb < 1024 ? Commons.I18n.formatNumber(mb, 0) + " MB" : root.gb(mb)
  }

  function ticked(item) {
    var p = root.selectPending[item.key]
    return p !== undefined ? p : item.selected
  }

  function paused(item) {
    var p = root.itemPending[item.key]
    return p !== undefined ? p === "pause" : item.paused
  }

  readonly property int tickedMB: root.items.reduce(function(sum, i) {
    return sum + (!i.paused && root.ticked(i) ? i.mb : 0)
  }, 0)
  readonly property int tickedVram: root.items.reduce(function(sum, i) {
    return sum + (!i.paused && root.ticked(i) ? (i.vram || 0) : 0)
  }, 0)
  readonly property int tickedCount: root.items.filter(function(i) { return !i.paused && root.ticked(i) }).length

  readonly property string freeLine: root.mem ? root.gb(root.mem.availableMB) + " of " + root.gb(root.mem.totalMB) + " RAM free" : ""
  readonly property string vramFreeLine: root.gpu
    ? root.size(Math.max(0, root.gpu.totalMB - root.gpu.usedMB)) + " of " + root.size(root.gpu.totalMB) + " VRAM free" : ""

  // One line for RAM, one for VRAM (when there's a GPU to read).
  readonly property string stateLine: {
    if (!root.status) return "Reading…"
    if (root.pending === "on") return "Freeing memory…"
    if (root.pending === "off") return "Bringing everything back…"
    var ram = root.freeLine, vram = root.vramFreeLine
    if (root.gameOn) {
      var since = root.status.since ? new Date(root.status.since * 1000) : null
      if (root.status.freedMB) ram += " · freed " + root.gb(root.status.freedMB)
      if (vram && root.status.freedVramMB) vram += " · freed " + root.size(root.status.freedVramMB)
      return (since ? "On since " + since.toLocaleTimeString(Qt.locale(Commons.I18n.formatLocale), "HH:mm") + "\n" : "")
        + ram + (vram ? "\n" + vram : "")
    }
    if (root.tickedCount === 0) return ram + (vram ? "\n" + vram : "") + "\nTick what to pause"
    return ram + " · frees about " + root.gb(root.tickedMB)
      + (vram ? "\n" + vram + (root.tickedVram > 0 ? " · frees about " + root.size(root.tickedVram) : "") : "")
  }

  function rowDescription(item) {
    var p = root.itemPending[item.key]
    if (p === "pause") return item.kind === "app" ? "Closing…" : "Stopping…"
    if (p === "resume") return item.kind === "app" ? "Reopening…" : "Starting…"
    return item.detail
  }

  readonly property string tooltipLabel: {
    if (!root.status) return "Game mode"
    if (root.working) return root.gameOn ? "Game mode: working…" : "Game mode: bringing everything back…"
    return (root.gameOn ? "Game mode on" : "Game mode off") + "\n" + root.freeLine
      + (root.vramFreeLine ? "\n" + root.vramFreeLine : "")
      + "\nMiddle-click to turn it " + (root.gameOn ? "off" : "on")
  }

  function refresh() {
    if (readProc.running) root.refreshQueued = true
    else readProc.running = true
  }

  function applyReading(text) {
    var data
    try { data = JSON.parse(text) } catch (e) { return }
    root.status = data
    if (data.busy) return
    if (root.pending !== "" && data.on === (root.pending === "on")) root.pending = ""
    // An item switch is done once the helper let go and the item shows the new state.
    var left = {}
    for (var k in root.itemPending) {
      var want = root.itemPending[k] === "pause"
      var item = data.items.find(function(i) { return i.key === k })
      if (item && item.paused !== want) left[k] = root.itemPending[k]
    }
    root.itemPending = left
    if (!selectProc.running) root.selectPending = ({})
  }

  // Runs gamemode.py detached, so it carries on even if the shell restarts.
  function launch(args) {
    Quickshell.execDetached(["setsid", "-f", "python3", root.scriptPath].concat(args))
    giveUp.restart()
  }

  function setGameMode(on) {
    if (root.working || !root.status) return
    if (on && root.tickedCount === 0) {
      root.popupOpen = true
      return
    }
    root.pending = on ? "on" : "off"
    root.launch([on ? "on" : "off"])
  }

  function toggleGameMode() {
    root.setGameMode(!root.gameOn)
  }

  function clickItem(item) {
    if (root.gameOn) {
      // While game mode is on a switch acts at once: pause one more, or bring one back.
      if (root.working) return
      var p = Object.assign({}, root.itemPending)
      p[item.key] = root.paused(item) ? "resume" : "pause"
      root.itemPending = p
      root.launch([p[item.key], item.key])
      return
    }
    var s = Object.assign({}, root.selectPending)
    s[item.key] = !root.ticked(item)
    root.selectPending = s
    selectProc.command = ["python3", root.scriptPath, "select", item.key, s[item.key] ? "on" : "off"]
    selectProc.running = true
  }

  // Contract KeyboardPanel expects on its `owner`.
  function close() {
    root.popupOpen = false
  }

  function toggle() {
    root.popupOpen = !root.popupOpen
    if (root.popupOpen) root.refresh()
  }

  // The floating bar can mount a hidden second copy of a widget; only the
  // copy on screen takes IPC.
  readonly property bool onScreen: {
    var w = button.QsWindow.window
    return !!w && w.visible && root.visible && root.width > 0 && root.height > 0
  }

  // `omarchy-shell io.github.yassinesawahi.gamemode <fn>` — bindable to keys.
  IpcHandler {
    target: "io.github.yassinesawahi.gamemode"
    enabled: root.onScreen

    function toggle() { root.toggle() }
    function close() { root.close() }
    function refresh() { root.refresh() }
    function toggleGameMode() { root.toggleGameMode() }
    function turnOn() { root.setGameMode(true) }
    function turnOff() { root.setGameMode(false) }
  }

  Process {
    id: readProc
    command: ["python3", root.scriptPath, "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyReading(text)
    }
    onExited: {
      if (!root.refreshQueued) return
      root.refreshQueued = false
      Qt.callLater(root.refresh)
    }
  }

  Process {
    id: selectProc
    onExited: root.refresh()
  }

  // If the helper never got going, stop showing it as busy.
  Timer {
    id: giveUp
    interval: 300000
    onTriggered: {
      root.pending = ""
      root.itemPending = ({})
    }
  }

  Timer {
    interval: root.working ? 1000 : root.popupOpen ? 3000 : 20000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    tooltipText: root.tooltipLabel
    fixedWidth: root.vertical ? -1 : glyph.width + button.scaledHorizontalMargin * 2
    fixedHeight: root.vertical ? Style.bar.iconSlot : -1

    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh()
      else if (b === Qt.MiddleButton) root.toggleGameMode()
      else root.toggle()
    }

    OpticalGlyph {
      id: glyph
      anchors.centerIn: parent
      width: Style.bar.iconCanvas
      height: Style.bar.iconCanvas
      text: root.padGlyph
      fontFamily: button.fontFamily
      fontSize: Style.bar.iconFont
      color: root.gameOn ? Commons.Color.accent : button.foreground
      opacity: root.working ? 0.75 : root.gameOn ? 1 : 0.5
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.popupOpen
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(330))
    contentHeight: popup.fittedContentHeight(col.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: col
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        readonly property color dim: Qt.darker(root.bar.foreground, 1.4)

        PanelSectionHeader {
          text: "GAME MODE"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
        }

        Item {
          width: parent.width
          height: Math.max(heroGlyph.implicitHeight, heroText.implicitHeight, powerButton.implicitHeight)

          Text {
            id: heroGlyph
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: root.padGlyph
            color: root.gameOn ? Commons.Color.accent : root.bar.foreground
            opacity: root.gameOn ? 1 : 0.5
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.title * 1.4
          }

          Column {
            id: heroText
            anchors.left: heroGlyph.right
            anchors.leftMargin: Style.space(10)
            anchors.right: powerButton.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              width: parent.width
              text: root.gameOn ? "Game mode is on" : "Game mode is off"
              textFormat: Text.PlainText
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
              elide: Text.ElideRight
            }
            Text {
              width: parent.width
              text: root.stateLine
              textFormat: Text.PlainText
              color: col.dim
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Button {
            id: powerButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: root.working ? "Wait…" : root.gameOn ? "Restore all" : "Pause ticked"
            enabled: !root.working && !!root.status && (root.gameOn || root.tickedCount > 0)
            selected: root.gameOn
            bordered: true
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            fontSize: Style.font.bodySmall
            onClicked: root.toggleGameMode()
          }
        }

        PanelSectionHeader {
          visible: root.items.length > 0
          text: root.gameOn ? "SWITCH ON TO PAUSE · OFF TO BRING BACK" : "TICK WHAT TO PAUSE"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
        }

        // Indexed rather than bound to the array, so a refresh doesn't rebuild
        // the rows under the pointer.
        Repeater {
          model: root.items.length

          delegate: Toggle {
            required property int index
            readonly property var item: root.items[index] || ({ key: "", label: "", detail: "", selected: false, paused: false })

            width: col.width
            label: item.label
            description: root.rowDescription(item)
            checked: root.gameOn ? root.paused(item) : root.ticked(item)
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            titleSize: Style.font.bodySmall
            onClicked: root.clickItem(item)
          }
        }

        Text {
          visible: !!root.status && root.items.length === 0
          width: parent.width
          text: "Nothing big is running."
          textFormat: Text.PlainText
          color: col.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }

        PanelSeparator {
          foreground: root.bar.foreground
        }

        Text {
          width: parent.width
          text: "Your ticks are remembered. Terminals, AI coding agents and game launchers are never listed. Middle-click the icon to switch game mode."
          textFormat: Text.PlainText
          color: col.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }
    }
  }
}
