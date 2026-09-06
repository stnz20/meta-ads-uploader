// ============================================================
// Meta Ads Uploader — Batch-Upload
// ============================================================

const BATCH_TAB        = "📋 Batch-Upload";
const ADSETS_TAB       = "ad_sets";
const ADS_TAB          = "ads";
const META_CONFIG_TAB  = "meta_config";
const NAMING_TAB_GID   = 0 /* GID des Naming-Tabs eintragen */;
const NAMING_HDR_ROW   = 14;
const READY_DATE_HEADER = "Ready am";  // Spalte für Datum, an dem "Ready for upload?" = yes gesetzt wurde

const ROW_DATUM      = 3;
const ROW_TABLE_HDR  = 6;
const ROW_TABLE_DATA = 7;
const MAX_TABLE_ROWS = 50;

// Tabellen-Spalten (1-basiert)
const TC_OUT_ADSET   = 1;   // A — Output Ad Set Name
const TC_REF         = 2;   // B — Referenz-Ad Set ID (source_adset_id)
const TC_CAMP        = 3;   // C — Kampagne-ID (auto)
const TC_CTA         = 4;   // D — CTA (auto)
const TC_SRC         = 5;   // E — Referenz-ID (auto)
const TC_PAGE        = 6;   // F — Page (auto, aus Referenz)
const TC_IG          = 7;   // G — Instagram (auto, aus Referenz)
const TC_ADVERTISER  = 8;   // H — Advertiser (optional, Dropdown aus meta_config)
const TC_PAGE_OVR    = 9;   // I — Facebook Page Override (optional)
const TC_IG_OVR      = 10;  // J — Instagram Override (optional)
const TC_DISPLAY_LNK = 11;  // K — Display Link (optional, sonst aus Referenz)
const TC_ADS_COUNT   = 12;  // L — # Ads
const TC_STATUS      = 13;  // M — Status

const ADSET_COLS = [
  "source_campaign_id","ad_set_name","daily_budget",
  "start_time","end_time","targeting_override",
  "page_id","source_adset_id","status","created_id",
];
const ADS_COLS = [
  "ad_set_ref","ad_name","image_url","image_hash","video_id","thumbnail_url",
  "body","body_2","body_3","body_4","body_5",
  "headline","headline_2","headline_3",
  "description","description_2","description_3",
  "cta","destination_url","display_link","instagram_actor_id",
  "page_id","advertiser_id",
  "status","created_id",
];


// ============================================================
// onOpen
// ============================================================

function onOpen() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss.getSheetByName(BATCH_TAB)) _buildBatchTab(ss);
  SpreadsheetApp.getUi()
    .createMenu("🚀 Ads Uploader")
    .addItem("Zeilen generieren",         "generateRows")
    .addItem("Tabelle aktualisieren",     "refreshTable")
    .addItem("Upload als DONE markieren", "markUploaded")
    .addSeparator()
    .addItem("Meta Config Info",          "showMetaConfigInfo")
    .addSeparator()
    .addItem("Diagnose",                  "diagnose")
    .addItem("Batch-Tab zurücksetzen",    "resetBatchTab")
    .addToUi();
}


// ============================================================
// onEdit — Referenz gewählt → auto-befüllen (1 Batch-Write)
// ============================================================

function onEdit(e) {
  try {
    const ws = e.range.getSheet();
    if (ws.getName() === BATCH_TAB)        { _onBatchEdit(e, ws);  return; }
    if (ws.getSheetId() === NAMING_TAB_GID) { _onNamingEdit(e, ws); return; }
  } catch (_) {}
}

// Batch-Tab: Referenz gewählt → Auto-Spalten befüllen
function _onBatchEdit(e, ws) {
  const row = e.range.getRow();
  const col = e.range.getColumn();
  if (col !== TC_REF || row < ROW_TABLE_DATA || row >= ROW_TABLE_DATA + MAX_TABLE_ROWS) return;
  const ref = _lookupRef(e.source, e.value);
  ws.getRange(row, TC_CAMP, 1, 5).setValues([[ref.campId, ref.cta, ref.srcId, ref.pageId, ref.igId]]);
}

// Naming-Tab: "Ready for upload?" = yes → aktuelles Datum in Spalte "Ready am" setzen
function _onNamingEdit(e, ws) {
  const { hdrRow, colMap } = _namingHeaderInfo(ws);
  if (hdrRow < 0) return;
  const readyCol = colMap["Ready for upload?"];
  const dateCol  = colMap[READY_DATE_HEADER];
  if (readyCol == null || dateCol == null) return;

  const startRow = e.range.getRow();
  const numRows  = e.range.getNumRows();
  const startCol = e.range.getColumn();
  const lastCol  = startCol + e.range.getNumColumns() - 1;
  const readyColNum = readyCol + 1;  // 1-basiert
  // Nur weiter, wenn der editierte Bereich die "Ready for upload?"-Spalte berührt
  if (readyColNum < startCol || readyColNum > lastCol) return;

  const today = _todayDe();
  for (let r = startRow; r < startRow + numRows; r++) {
    if (r <= hdrRow) continue;  // Header-Zeile auslassen
    const val      = String(ws.getRange(r, readyColNum).getValue() || "").trim().toLowerCase();
    const dateCell = ws.getRange(r, dateCol + 1);
    if (val === "yes") {
      if (!String(dateCell.getValue() || "").trim()) dateCell.setValue(today);
    } else {
      dateCell.clearContent();
    }
  }
}


// ============================================================
// Referenz nachschlagen (gecacht pro Aufruf)
// ============================================================

function _lookupRef(ss, refAdSetId) {
  if (!refAdSetId) return { campId:"", cta:"", srcId:"", pageId:"", igId:"" };
  const adSetsWs = ss.getSheetByName(ADSETS_TAB);
  const adsWs    = ss.getSheetByName(ADS_TAB);
  const adSetRow = _findRow(adSetsWs, "source_adset_id", refAdSetId);
  // ID nicht im Sheet → trotzdem als srcId zurückgeben, campId bleibt leer (manuell eintragen)
  if (!adSetRow) return { campId:"", cta:"", srcId: refAdSetId, pageId:"", igId:"" };
  // Für CTA/Instagram: zugehörige Ad-Zeile über ad_set_name finden
  const adSetName = adSetRow.ad_set_name || "";
  const adRow     = adSetName ? _findRow(adsWs, "ad_set_ref", adSetName) : null;
  return {
    campId: adSetRow.source_campaign_id || "",
    srcId:  refAdSetId,
    pageId: adSetRow.page_id || "",
    cta:    adRow?.cta || "",
    igId:   adRow?.instagram_actor_id || "",
  };
}


// ============================================================
// Tabelle aktualisieren
// ============================================================

function refreshTable() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ws = ss.getSheetByName(BATCH_TAB);
  if (!ws) return;
  _fillTable(ss, ws);
  SpreadsheetApp.getUi().alert("✅ Tabelle aktualisiert.");
}

function _fillTable(ss, ws) {
  // Bestehende Werte (Referenz + Overrides) per Batch-Read merken
  const existing = {};
  if (ws.getLastRow() >= ROW_TABLE_DATA) {
    const saved = ws.getRange(ROW_TABLE_DATA, TC_OUT_ADSET, MAX_TABLE_ROWS, TC_STATUS).getValues();
    saved.forEach(r => {
      const name = String(r[TC_OUT_ADSET-1]||"").trim();
      if (!name) return;
      existing[name] = {
        ref:         String(r[TC_REF-1]        ||"").trim(),
        advertiser:  String(r[TC_ADVERTISER-1] ||"").trim(),
        pageOvr:     String(r[TC_PAGE_OVR-1]   ||"").trim(),
        igOvr:       String(r[TC_IG_OVR-1]     ||"").trim(),
        displayLink: String(r[TC_DISPLAY_LNK-1]||"").trim(),
      };
    });
  }

  // Tabellenbereich leeren (1 API-Call)
  ws.getRange(ROW_TABLE_DATA, 1, MAX_TABLE_ROWS, TC_STATUS)
    .clearContent().clearDataValidations()
    .setBackground("#ffffff").setFontColor("#000000").setFontStyle("normal");

  const readyRows = _getReadyRows(ss);
  const groups    = {};
  readyRows.forEach(r => {
    if (!groups[r.adSetName]) groups[r.adSetName] = [];
    groups[r.adSetName].push(r);
  });
  const adSetNames = Object.keys(groups);

  if (adSetNames.length === 0) {
    ws.getRange(ROW_TABLE_DATA, TC_OUT_ADSET, 1, 2).merge()
      .setValue("Keine Zeilen bereit — im Naming Tab 'Ready for upload?' = yes setzen")
      .setFontColor("#e53935").setFontStyle("italic");
    return;
  }

  const refNames = _doneAdSetNames(ss);
  const refRule  = refNames.length
    ? SpreadsheetApp.newDataValidation().requireValueInList(refNames, true).setAllowInvalid(true).build()
    : null;

  // Alle Werte als Batch vorbereiten
  const outVals  = [];
  const cntVals  = [];

  adSetNames.forEach((adSetName, i) => {
    outVals.push([adSetName]);
    cntVals.push([groups[adSetName].length]);
  });

  const n = adSetNames.length;

  // Batch-Writes
  ws.getRange(ROW_TABLE_DATA, TC_OUT_ADSET, n, 1).setValues(outVals)
    .setBackground("#f8f9fa").setFontColor("#1a1a1a");
  ws.getRange(ROW_TABLE_DATA, TC_ADS_COUNT, n, 1).setValues(cntVals)
    .setHorizontalAlignment("center").setFontColor("#5f6368");

  // Meta-Config-Dropdowns laden (Advertiser, Page, Instagram)
  const { advertiserRule, pageRule, igRule } = _metaConfigRules(ss);

  // Referenz-Dropdown + Styling + Override-Dropdowns pro Zeile
  adSetNames.forEach((adSetName, i) => {
    const row     = ROW_TABLE_DATA + i;
    const refCell = ws.getRange(row, TC_REF);
    if (refRule) refCell.setDataValidation(refRule);
    refCell.setBackground(i % 2 === 0 ? "#e8f0fe" : "#dce8fc")
      .setBorder(true,true,true,true,false,false,"#4a86e8",SpreadsheetApp.BorderStyle.SOLID);

    // Auto-Spalten stylen (C-G)
    ws.getRange(row, TC_CAMP, 1, 5)
      .setBackground("#f8f9fa").setFontColor("#5f6368").setFontStyle("italic");

    // Override-Spalten stylen (H-K)
    ws.getRange(row, TC_ADVERTISER, 1, 4)
      .setBackground(i % 2 === 0 ? "#f1f8e9" : "#e8f5e9").setFontColor("#33691e");
    if (advertiserRule) ws.getRange(row, TC_ADVERTISER).setDataValidation(advertiserRule);
    if (pageRule)       ws.getRange(row, TC_PAGE_OVR).setDataValidation(pageRule);
    if (igRule)         ws.getRange(row, TC_IG_OVR).setDataValidation(igRule);

    // Gespeicherte Referenz + Overrides wiederherstellen
    const saved = existing[adSetName];
    if (saved) {
      refCell.setValue(saved.ref);
      if (saved.ref) {
        const ref = _lookupRef(ss, saved.ref);
        ws.getRange(row, TC_CAMP, 1, 5).setValues([[ref.campId, ref.cta, ref.srcId, ref.pageId, ref.igId]]);
      }
      if (saved.advertiser)  ws.getRange(row, TC_ADVERTISER).setValue(saved.advertiser);
      if (saved.pageOvr)     ws.getRange(row, TC_PAGE_OVR).setValue(saved.pageOvr);
      if (saved.igOvr)       ws.getRange(row, TC_IG_OVR).setValue(saved.igOvr);
      if (saved.displayLink) ws.getRange(row, TC_DISPLAY_LNK).setValue(saved.displayLink);
    }
  });
}


// ============================================================
// Zeilen generieren
// ============================================================

function generateRows() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ui = SpreadsheetApp.getUi();
  const ws = ss.getSheetByName(BATCH_TAB);
  if (!ws) { ui.alert("Tab fehlt — bitte Sheet neu laden."); return; }

  const datum = String(ws.getRange(ROW_DATUM, 2).getValue() || "").trim();
  if (!datum) { ui.alert("Startdatum fehlt (Zeile 3)."); return; }

  // Tabelle per Batch lesen
  const tableVals = ws.getRange(ROW_TABLE_DATA, 1, MAX_TABLE_ROWS, TC_STATUS).getValues();

  const adSetsWs = ss.getSheetByName(ADSETS_TAB);
  const adsWs    = ss.getSheetByName(ADS_TAB);
  const refCache = {};
  const _getRef  = name => {
    if (!refCache[name]) refCache[name] = _lookupRef(ss, name);
    return refCache[name];
  };

  const toProcess = [];
  const errs      = [];
  const autoUpdates = []; // [row, campId, cta, srcId, pageId, igId]

  tableVals.forEach((row, i) => {
    const outAdSet    = String(row[TC_OUT_ADSET   - 1] || "").trim();
    const refAdSet    = String(row[TC_REF         - 1] || "").trim();
    const campIdCell  = String(row[TC_CAMP        - 1] || "").trim();  // manuell eingetragen falls neue ID
    const advertiser  = String(row[TC_ADVERTISER  - 1] || "").trim();
    const pageOvr     = String(row[TC_PAGE_OVR    - 1] || "").trim();
    const igOvr       = String(row[TC_IG_OVR      - 1] || "").trim();
    const displayLink = String(row[TC_DISPLAY_LNK - 1] || "").trim();
    if (!outAdSet || !refAdSet) return;

    const ref      = _getRef(refAdSet);
    const tableRow = ROW_TABLE_DATA + i;

    // Kampagnen-ID: aus Lookup oder manuell in Spalte C eingetragen
    const resolvedCampId = ref.campId || campIdCell;
    if (!resolvedCampId) {
      errs.push("Kampagnen-ID fehlt für \"" + refAdSet + "\" — bitte in Spalte C (↳ Kampagne-ID) eintragen");
      return;
    }
    // CTA nur prüfen wenn die Referenz im Sheet bekannt war — bei neuen IDs liefert apply_defaults den CTA
    if (!ref.cta && ref.campId) { errs.push("CTA fehlt für Referenz: \"" + refAdSet + "\" — 'cta' Spalte in 'ads' prüfen"); return; }
    autoUpdates.push([tableRow, resolvedCampId, ref.cta, ref.srcId, ref.pageId, ref.igId]);
    toProcess.push({ outAdSet, refAdSet, advertiser, pageOvr, igOvr, displayLink, ...ref, campId: resolvedCampId, tableRow });
  });

  // Auto-Spalten per Batch schreiben (visuelles Feedback)
  autoUpdates.forEach(([r, campId, cta, srcId, pageId, igId]) => {
    ws.getRange(r, TC_CAMP, 1, 5).setValues([[campId, cta, srcId, pageId, igId]]);
  });

  if (errs.length > 0) { ui.alert("⚠️ Fehler:\n\n• " + errs.join("\n• ")); return; }
  if (toProcess.length === 0) {
    ui.alert("Keine Zeilen bereit.\n\nBitte in Spalte B 'Referenz-Ad Set' wählen.");
    return;
  }

  const summary = toProcess.map(p => "• " + p.outAdSet + "\n  → " + p.refAdSet).join("\n");
  if (ui.alert("🚀 " + toProcess.length + " Ad Set(s) generieren?",
    summary + "\n\nIn 'ad_sets' und 'ads' schreiben?",
    ui.ButtonSet.YES_NO) !== ui.Button.YES) return;

  const startTime   = _isoDate(datum);
  const readyRows   = _getReadyRows(ss);
  const adSetsHdrs  = _headers(adSetsWs);
  const adsHdrs     = _headers(adsWs);
  const namingWs    = _getNamingSheet(ss);
  const adSetsToAdd = [];
  const adsToAdd    = [];
  const namingUpdates = []; // [sheetRow, col]
  const statusUpdates = []; // [tableRow, value]
  let   totalAds    = 0;

  toProcess.forEach(p => {
    const adsForSet = readyRows.filter(r => r.adSetName === p.outAdSet);

    adSetsToAdd.push(_makeRow(adSetsHdrs, ADSET_COLS, {
      source_campaign_id: p.campId,
      ad_set_name:        p.outAdSet,
      daily_budget:       "",
      start_time:         startTime,
      end_time:           "",
      targeting_override: "",
      page_id:            p.pageId,
      source_adset_id:    p.srcId,
      status:             "",
      created_id:         "",
    }));

    // Effektive Page-ID und Instagram-ID (Override hat Vorrang)
    const effectivePage = p.pageOvr || p.pageId;
    const effectiveIg   = p.igOvr   || p.igId;

    adsForSet.forEach(r => {
      adsToAdd.push(_makeRow(adsHdrs, ADS_COLS, {
        ad_set_ref:         r.adSetName,
        ad_name:            r.adName,
        image_url:          r.assetLink,
        image_hash:"", video_id:"", thumbnail_url:"",
        body:"", body_2:"", body_3:"", body_4:"", body_5:"",
        headline:"", headline_2:"", headline_3:"",
        description:"", description_2:"", description_3:"",
        cta:                p.cta,
        destination_url:    r.lpUrl,
        display_link:       p.displayLink,  // leer → apply_defaults füllt aus Referenz
        instagram_actor_id: effectiveIg,
        page_id:            effectivePage,
        advertiser_id:      p.advertiser,
        status:"", created_id:"",
      }));
      if (namingWs && r.sheetRow > 0) {
        const col = r.colMap["Uploaded?"];
        if (col >= 0) namingUpdates.push([r.sheetRow, col + 1]);
      }
      totalAds++;
    });

    statusUpdates.push([p.tableRow, "✅ QUEUED"]);
  });

  // Batch-Writes
  adSetsToAdd.forEach(row => adSetsWs.appendRow(row));
  adsToAdd.forEach(row => adsWs.appendRow(row));
  namingUpdates.forEach(([r, c]) => namingWs.getRange(r, c).setValue("QUEUED"));
  statusUpdates.forEach(([r, v]) => ws.getRange(r, TC_STATUS).setValue(v).setFontColor("#2e7d32").setFontWeight("bold"));

  ui.alert("✅ Fertig!\n\n" +
    toProcess.length + " Ad Set(s), " + totalAds + " Ad(s) geschrieben.\n" +
    "Naming Tab: " + totalAds + " Zeile(n) → QUEUED\n\n" +
    "Jetzt: python3 main.py --dry-run");
}


// ============================================================
// Upload als DONE markieren
// ============================================================

function markUploaded() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ui = SpreadsheetApp.getUi();
  const namingWs = _getNamingSheet(ss);
  if (!namingWs) { ui.alert("Naming Tab nicht gefunden."); return; }

  const { colMap, dataRows } = _readNamingRows(namingWs);
  const uploadedCol  = colMap["Uploaded?"];
  const uploadedWhen = colMap["Uploaded When?"];
  if (uploadedCol < 0) { ui.alert("Spalte 'Uploaded?' nicht gefunden."); return; }

  const today   = _todayIso();
  let   count   = 0;
  dataRows.forEach(({ values, sheetRow }) => {
    if (String(values[uploadedCol]||"").trim().toUpperCase() === "QUEUED") {
      namingWs.getRange(sheetRow, uploadedCol + 1).setValue("DONE");
      if (uploadedWhen >= 0) namingWs.getRange(sheetRow, uploadedWhen + 1).setValue(today);
      count++;
    }
  });
  ui.alert(count > 0 ? "✅ " + count + " Zeile(n) als DONE markiert." : "Keine QUEUED-Zeilen gefunden.");
}


// ============================================================
// Diagnose
// ============================================================

function diagnose() {
  const ss    = SpreadsheetApp.getActiveSpreadsheet();
  const lines = ["Alle Tabs:"];
  ss.getSheets().forEach(s => lines.push("  '" + s.getName() + "'  GID=" + s.getSheetId()));

  const nws = _getNamingSheet(ss);
  lines.push("\nNaming Tab: " + (nws ? "✅ '" + nws.getName() + "'" : "❌ nicht gefunden"));

  if (nws) {
    // Show auto-detected header row
    const scanRows = Math.min(nws.getLastRow(), 30);
    const allData  = nws.getRange(1, 1, scanRows, nws.getLastColumn()).getValues();
    let hdrIdx = -1;
    for (let i = 0; i < allData.length; i++) {
      if (allData[i].some(c => String(c||"").trim() === "Ready for upload?")) { hdrIdx = i; break; }
    }
    if (hdrIdx < 0) {
      lines.push("❌ Header-Zeile nicht gefunden (kein 'Ready for upload?' in Zeilen 1-30)");
    } else {
      lines.push("Header auto-erkannt in Zeile " + (hdrIdx+1) + ":");
      allData[hdrIdx].forEach((h,i) => { if(String(h).trim()) lines.push("  Col"+(i+1)+": '"+String(h).trim()+"'"); });
    }

    const { colMap, dataRows } = _readNamingRows(nws);
    const rc  = colMap["Ready for upload?"];
    const uc  = colMap["Uploaded?"];
    const asc = colMap["Output Ad Set Naming →"];
    const anc = colMap["Output Ad Naming →"];
    lines.push("\nSpalten-Indizes: rc=" + rc + " asc=" + asc + " anc=" + anc + " uc=" + uc);
    const ready = dataRows.filter(({values}) => String(values[rc]||"").trim().toLowerCase()==="yes" && !String(values[uc]||"").trim());
    lines.push("Bereit für Upload (ready=yes, kein uploaded): " + ready.length + " Zeile(n)");
    const withNames = ready.filter(({values}) => String(values[asc]||"").trim() && String(values[anc]||"").trim());
    lines.push("Davon vollständig (Ad-Set + Ad-Name): " + withNames.length + " Zeile(n) → werden verarbeitet");
    const missingAdSet = ready.filter(({values}) => !String(values[asc]||"").trim() && String(values[anc]||"").trim());
    if (missingAdSet.length > 0) {
      lines.push("\n⚠️ " + missingAdSet.length + " Zeile(n) fehlt 'Output Ad Set Naming →' (Formel prüfen!):");
      missingAdSet.slice(0,5).forEach(r => lines.push("  Zeile " + r.sheetRow + ": " + (String(r.values[anc]||"").trim() || "(leer)")));
    }
  }

  SpreadsheetApp.getUi().alert(lines.join("\n"));
}


// ============================================================
// Tab aufbauen
// ============================================================

function _buildBatchTab(ss) {
  const ws = ss.insertSheet(BATCH_TAB, 0);
  ws.setColumnWidth(TC_OUT_ADSET,   360);
  ws.setColumnWidth(TC_REF,         200);
  ws.setColumnWidth(TC_CAMP,        155);
  ws.setColumnWidth(TC_CTA,          75);
  ws.setColumnWidth(TC_SRC,         155);
  ws.setColumnWidth(TC_PAGE,         80);
  ws.setColumnWidth(TC_IG,          120);
  ws.setColumnWidth(TC_ADVERTISER,  160);
  ws.setColumnWidth(TC_PAGE_OVR,    160);
  ws.setColumnWidth(TC_IG_OVR,      160);
  ws.setColumnWidth(TC_DISPLAY_LNK, 130);
  ws.setColumnWidth(TC_ADS_COUNT,    50);
  ws.setColumnWidth(TC_STATUS,      110);

  ws.setRowHeight(1, 42);
  ws.getRange(1, 1, 1, TC_STATUS).merge()
    .setValue("📋 Batch-Upload — Meta Ads generieren")
    .setBackground("#1c4587").setFontColor("#ffffff")
    .setFontSize(13).setFontWeight("bold").setVerticalAlignment("middle");
  ws.setRowHeight(2, 8);

  ws.getRange(3, 1).setValue("Startdatum").setFontWeight("bold").setBackground("#fce8b2");
  ws.getRange(3, 2).setValue(_todayIso() + "T10:00:00").setBackground("#ffffff")
    .setBorder(true,true,true,true,false,false,"#f9ab00",SpreadsheetApp.BorderStyle.SOLID);
  ws.setRowHeight(4, 8);

  ws.getRange(5, 1, 1, TC_STATUS).merge()
    .setValue("Spalte B ausfüllen → 🚀 Ads Uploader → Zeilen generieren  |  Neue Ads: Tabelle aktualisieren")
    .setBackground("#e8eaf6").setFontColor("#3949ab").setFontSize(10).setHorizontalAlignment("center");
  ws.setRowHeight(5, 24);

  ws.setRowHeight(ROW_TABLE_HDR, 28);
  ws.getRange(ROW_TABLE_HDR, 1, 1, TC_STATUS).setValues([[
    "Output Ad Set  (aus Naming Tab)",
    "Referenz-Ad Set  ← hier wählen",
    "↳ Kampagne-ID","↳ CTA","↳ Ref-ID","↳ Page (Ref)","↳ IG (Ref)",
    "Advertiser","Page Override","IG Override","Display Link",
    "# Ads","Status"
  ]]).setBackground("#37474f").setFontColor("#ffffff").setFontWeight("bold").setVerticalAlignment("middle");

  _fillTable(ss, ws);
  return ws;
}

function resetBatchTab() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ui = SpreadsheetApp.getUi();
  if (ui.alert("Batch-Tab zurücksetzen?","Referenz-Zuweisungen gehen verloren.",ui.ButtonSet.YES_NO) !== ui.Button.YES) return;
  const ex = ss.getSheetByName(BATCH_TAB);
  if (ex) ss.deleteSheet(ex);
  _buildBatchTab(ss);
  ui.alert("✅ Zurückgesetzt.");
}


// ============================================================
// Meta Config (Pages, Instagram, Advertiser)
// ============================================================

function showMetaConfigInfo() {
  const ss  = SpreadsheetApp.getActiveSpreadsheet();
  const cfg = ss.getSheetByName(META_CONFIG_TAB);
  const count = cfg ? Math.max(0, cfg.getLastRow() - 1) : 0;
  SpreadsheetApp.getUi().alert(
    "Meta Config — " + count + " Einträge geladen\n\n" +
    "Um Pages, Instagram-Konten und Advertiser zu laden:\n" +
    "  python3 main.py --fetch-meta-config\n\n" +
    "Danach 'Tabelle aktualisieren' ausführen."
  );
}

function _metaConfigRules(ss) {
  const cfg = ss.getSheetByName(META_CONFIG_TAB);
  if (!cfg || cfg.getLastRow() < 2) return { advertiserRule: null, pageRule: null, igRule: null };

  const rows = cfg.getRange(2, 1, cfg.getLastRow()-1, 3).getValues();
  const advertisers = [], pages = [], igs = [];
  rows.forEach(r => {
    const type = String(r[0]||"").trim().toLowerCase();
    const id   = String(r[1]||"").trim();
    const name = String(r[2]||"").trim();
    if (!id) return;
    const label = id + (name ? " — " + name : "");
    if (type === "advertiser") advertisers.push(label);
    if (type === "page")       pages.push(label);
    if (type === "instagram")  igs.push(label);
  });

  const _rule = arr => arr.length
    ? SpreadsheetApp.newDataValidation().requireValueInList(arr, true).setAllowInvalid(true).build()
    : null;

  return { advertiserRule: _rule(advertisers), pageRule: _rule(pages), igRule: _rule(igs) };
}


// ============================================================
// Naming Tab lesen
// ============================================================

function _getReadyRows(ss) {
  const nws = _getNamingSheet(ss);
  if (!nws) return [];
  const { colMap, dataRows } = _readNamingRows(nws);
  const rc  = colMap["Ready for upload?"];
  const uc  = colMap["Uploaded?"];
  const asc = colMap["Output Ad Set Naming →"];
  const anc = colMap["Output Ad Naming →"];
  const alc = colMap["Asset Link"];
  const luc = colMap["LP URL"];
  const lic = colMap["LP ID"];
  if (rc == null || asc == null || anc == null) return [];

  const rows = dataRows
    .filter(({values}) =>
      String(values[rc]||"").trim().toLowerCase()==="yes" &&
      !String(values[uc]||"").trim()
    )
    .map(({values, sheetRow}) => ({
      adSetName: String(values[asc]||"").trim(),
      adName:    String(values[anc]||"").trim(),
      assetLink: alc != null ? String(values[alc]||"").trim() : "",
      lpUrl:     luc != null ? String(values[luc]||"").trim() : "",
      lpId:      lic != null ? String(values[lic]||"").trim() : "",
      sheetRow, colMap,
    }))
    .filter(r => r.adSetName && r.adName);

  // Normalize: group by concept (adSetName without MetaAd-ID),
  // assign V1's adSetName (lowest MetaAd number) to all versions in the group.
  const conceptGroups = {};
  rows.forEach(r => {
    const key = r.adSetName.replace(/_MetaAd-\d+_/, "_");
    if (!conceptGroups[key]) conceptGroups[key] = [];
    conceptGroups[key].push(r);
  });
  Object.values(conceptGroups).forEach(g => {
    if (g.length <= 1) return;
    g.sort((a, b) => {
      const na = parseInt((a.adSetName.match(/MetaAd-(\d+)/) || [,0])[1]);
      const nb = parseInt((b.adSetName.match(/MetaAd-(\d+)/) || [,0])[1]);
      return na - nb;
    });
    const v1Name = g[0].adSetName;
    g.slice(1).forEach(r => { r.adSetName = v1Name; });
  });

  return rows;
}

// Header-Zeile (1-basiert) + Spalten-Map ermitteln: scannt die ersten 30 Zeilen nach "Ready for upload?"
function _namingHeaderInfo(ws) {
  const lastRow = ws.getLastRow();
  const lastCol = ws.getLastColumn();
  if (lastRow < 1 || lastCol < 1) return { hdrRow: -1, colMap: {} };

  const scanRows = Math.min(lastRow, 30);
  const allData  = ws.getRange(1, 1, scanRows, lastCol).getValues();
  let hdrIdx = -1;
  for (let i = 0; i < allData.length; i++) {
    if (allData[i].some(c => String(c||"").trim() === "Ready for upload?")) {
      hdrIdx = i; break;
    }
  }
  if (hdrIdx < 0) return { hdrRow: -1, colMap: {} };

  const colMap = {};
  allData[hdrIdx].forEach((h,i) => { const k=String(h||"").trim(); if(k) colMap[k]=i; });
  return { hdrRow: hdrIdx + 1, colMap };  // hdrRow 1-basiert
}

function _readNamingRows(ws) {
  const { hdrRow, colMap } = _namingHeaderInfo(ws);
  if (hdrRow < 0) return { colMap:{}, dataRows:[] };

  const lastRow = ws.getLastRow();
  const lastCol = ws.getLastColumn();
  const ds = hdrRow + 1; // 1-based sheet row of first data row
  if (lastRow < ds) return { colMap, dataRows:[] };
  const raw = ws.getRange(ds, 1, lastRow-ds+1, lastCol).getValues();
  return { colMap, dataRows: raw.map((values,i) => ({ values, sheetRow: ds+i })) };
}

function _getNamingSheet(ss) {
  return ss.getSheets().find(s => s.getSheetId() === NAMING_TAB_GID) ||
         ss.getSheetByName("New naming convention") || null;
}


// ============================================================
// Hilfsfunktionen
// ============================================================

function _doneAdSetNames(ss) {
  const ws = ss.getSheetByName(ADSETS_TAB);
  if (!ws || ws.getLastRow() < 2) return [];
  const hdrs = _headers(ws);
  const ii = hdrs.indexOf("source_adset_id");
  const si = hdrs.indexOf("status");
  if (ii < 0) return [];
  return ws.getRange(2,1,ws.getLastRow()-1,hdrs.length).getValues()
    .filter(r => String(r[si]||"").trim().toUpperCase()==="DONE")
    .map(r => String(r[ii]||"").trim()).filter(v=>v).reverse();
}

function _findRow(ws, colName, val) {
  if (!ws || ws.getLastRow() < 2) return null;
  const hdrs = _headers(ws);
  const ci = hdrs.indexOf(colName);
  if (ci < 0) return null;
  for (const row of ws.getRange(2,1,ws.getLastRow()-1,hdrs.length).getValues()) {
    if (String(row[ci]||"").trim() === val) {
      const obj = {};
      hdrs.forEach((h,i) => { obj[h]=String(row[i]||"").trim(); });
      return obj;
    }
  }
  return null;
}

function _headers(ws) {
  return ws.getRange(1,1,1,ws.getLastColumn()).getValues()[0].map(h=>String(h).trim());
}

function _makeRow(hdrs, fallback, data) {
  const cols = hdrs.some(h=>h.length>0) ? hdrs : fallback;
  return cols.map(h => h in data ? data[h] : "");
}

function _todayIso() {
  const d = new Date();
  return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
}

function _todayDe() {
  const d = new Date();
  return String(d.getDate()).padStart(2,"0")+"."+String(d.getMonth()+1).padStart(2,"0")+"."+d.getFullYear();
}

function _isoDate(raw) {
  if (/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(raw)) return raw;
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw+"T10:00:00";
  return raw;
}
