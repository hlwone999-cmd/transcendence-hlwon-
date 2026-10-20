// Transcendence — journal.dart
// FITUR BARU: Jurnal Trade.
//
// Catat trade: tanggal + jam, pair, price entry, rules awal → rules akhir,
// bias arah trade (BUY/SELL), plus dua penanda manual per trade:
//   - HASIL    : TP / SL (otomatis mengikuti arah rules, tetap bisa diubah)
//   - EKSEKUSI : KEAMBIL / MISS — trade yang di-miss tidak dihitung TP/SL,
//                tidak mengubah balance, tapi tetap masuk total trade.
// Buangan hanya dari trade yang TURUN rules (SL) — dan yang dihitung
// BUANGAN MURNI saja (bukan gabungan SL + buangan):
//   - rules naik (akhir > awal)  = TP  (+ selisih balance antar rule)
//   - rules turun (akhir < awal) = SL  → buangan murni = balance setelah
//     SL (90%, rumus rDiv10 engine.dart) − balance rule tujuan.
//     (Selisih penuh antar rule = nominal SL 10% + buangan — jangan
//     tertukar; sama seperti "X buangan" di tab Kalkulator.)
//   - sama tanpa tanda (entry lama) = BE (0)
// Statistik: total trade, total TP, total SL, winrate, TOTAL BUANGAN,
// balance sekarang (berantai dari "Mulai dari Rule"), dan drawdown
// harian / mingguan / bulanan = akumulasi SL MURNI pada periode TERBURUK
// (hari / minggu / bulan dengan penumpukan SL terbesar — trade SL berapa
// pun tanggalnya langsung terlihat, tidak tergantung "hari ini" di jam
// HP), TOTAL = akumulasi seluruh SL murni (10% dari balance rule awal —
// TANPA buangan, SL berdiri sendiri; buangan dilaporkan terpisah di
// kartu TOTAL BUANGAN). Eksekusi juga diringkas di kartu KEAMBIL (trade
// yang benar-benar diambil) dan MISS. Data jurnal bisa di-EKSPOR ke file
// EXCEL (.xlsx — dibagikan lewat share sheet, bisa dibuka/diedit di Excel
// / Google Sheets) atau disalin sebagai teks JSON; IMPOR menerima file
// .xlsx maupun JSON tempelan — untuk backup atau pindah perangkat.
//
// Modul ini STANDALONE: tidak mengubah algoritma/kalkulator existing.
// Data disimpan di SharedPreferences (journal_entries & journal_start).
import 'dart:convert';
import 'dart:io'; // File — simpan xlsx ke temp sebelum dibagikan

import 'package:excel/excel.dart' hide Border, TextSpan; // baca + tulis .xlsx
import 'package:file_picker/file_picker.dart'; // pilih .xlsx saat impor
import 'package:flutter/material.dart';
import 'package:flutter/services.dart'; // Clipboard — salin hasil ekspor
import 'package:path_provider/path_provider.dart'; // folder temp
import 'package:share_plus/share_plus.dart'; // bagikan hasil ekspor
import 'package:shared_preferences/shared_preferences.dart';

import 'engine.dart';
import 'ui_kit.dart';

int ruleBal(int n) => kRuleBalances[n.clamp(1, kRuleBalances.length) - 1];

// ---------------------------------------------------------------------------
// Model
// ---------------------------------------------------------------------------

class JournalEntry {
  final DateTime at;
  final String pair;
  final double price;
  final int ruleA; // rules awal
  final int ruleB; // rules akhir
  final String mark; // 'TP' | 'SL' | '' (entry lama → diturunkan dari rules)
  final bool taken; // true = keambil, false = miss
  final String bias; // 'BUY' | 'SELL' | '' (entry lama tanpa bias)
  const JournalEntry({
    required this.at,
    required this.pair,
    required this.price,
    required this.ruleA,
    required this.ruleB,
    this.mark = '',
    this.taken = true,
    this.bias = '',
  });

  Map<String, dynamic> toJson() => {
        'at': at.toIso8601String(),
        'pair': pair,
        'price': price,
        'a': ruleA,
        'b': ruleB,
        'mark': mark,
        'taken': taken,
        'bias': bias,
      };

  static JournalEntry fromJson(Map<String, dynamic> j) => JournalEntry(
        at: DateTime.tryParse('${j['at']}') ?? DateTime.now(),
        pair: '${j['pair'] ?? '-'}',
        price: (j['price'] as num?)?.toDouble() ?? 0,
        ruleA: (j['a'] as num?)?.toInt() ?? 1,
        ruleB: (j['b'] as num?)?.toInt() ?? 1,
        mark: '${j['mark'] ?? ''}',
        taken: (j['taken'] as bool?) ?? true,
        bias: '${j['bias'] ?? ''}',
      );

  /// 1 = TP, -1 = SL, 0 = BE (entry lama tanpa tanda, rules sama),
  /// 2 = MISS (tidak diambil — bukan TP/SL, balance tidak berubah).
  int get outcome {
    if (!taken) return 2;
    if (mark == 'SL') return -1;
    if (mark == 'TP') return 1;
    return ruleB > ruleA ? 1 : (ruleB < ruleA ? -1 : 0);
  }

  /// P/L sesuai tangga rules: balance rule akhir − balance rule awal.
  /// Trade miss tidak mengubah balance.
  int get pnl => taken ? ruleBal(ruleB) - ruleBal(ruleA) : 0;

  /// Buangan MURNI saat SL turun rules — bukan selisih penuh antar rule.
  ///
  /// Selisih penuh rule(A) − rule(B) itu GABUNGAN: nominal SL (10% dari
  /// balance rule awal) + buangan murni. Buangan murni = balance setelah
  /// SL (90% dari balance rule awal, rumus rDiv10 engine.dart) − balance
  /// rule tujuan — definisi sama dengan "X buangan" di tab Kalkulator.
  int get buangan =>
      outcome < 0 && ruleB < ruleA ? rDiv10(ruleBal(ruleA), 9) - ruleBal(ruleB) : 0;

  /// Nominal SL MURNI saat trade SL turun rules — 10% dari balance rule
  /// awal (rumus engine.dart: sl = b − rDiv10(b, 9)), TANPA buangan.
  /// Dipakai untuk drawdown supaya nominal SL berdiri sendiri; buangan
  /// sudah dihitung terpisah lewat getter [buangan].
  int get slMurni => outcome < 0 && ruleB < ruleA
      ? ruleBal(ruleA) - rDiv10(ruleBal(ruleA), 9)
      : 0;
}

// ---------------------------------------------------------------------------
// Ekspor / Impor (fungsi murni — mudah dites)
// ---------------------------------------------------------------------------

/// Bungkus seluruh data jurnal jadi satu teks JSON — untuk backup atau
/// pindah perangkat (salin ke clipboard / dibagikan sebagai teks).
String journalExportJson(List<JournalEntry> entries, int startRule,
    {DateTime? exportedAt}) {
  return const JsonEncoder.withIndent('  ').convert({
    'app': 'transcendence',
    'type': 'journal',
    'version': 1,
    'exported': (exportedAt ?? DateTime.now()).toIso8601String(),
    'start': startRule,
    'count': entries.length,
    'entries': entries.map((e) => e.toJson()).toList(),
  });
}

/// Hasil impor: daftar trade + "mulai dari rule".
class JournalImport {
  final List<JournalEntry> entries;
  final int startRule;
  const JournalImport(this.entries, this.startRule);
}

/// Baca hasil ekspor. Menerima format amplop lengkap ({app, start,
/// entries}) ataupun daftar entry polosan (list) — start default 1 dan
/// di-clamp ke jumlah rule yang ada. Melempar FormatException kalau
/// bentuknya tidak dikenal (ditangkap UI → toast, tidak pernah crash).
JournalImport journalParseImport(String raw) {
  final d = jsonDecode(raw);
  List rawList;
  int start = 1;
  if (d is Map) {
    if (d['entries'] is! List) {
      throw const FormatException('bagian "entries" tidak ada / bukan list');
    }
    rawList = d['entries'] as List;
    start = (d['start'] as num?)?.toInt() ?? 1;
  } else if (d is List) {
    rawList = d;
  } else {
    throw const FormatException('bentuk data tidak dikenal');
  }
  final entries = rawList
      .whereType<Map>()
      .map((m) => JournalEntry.fromJson(Map<String, dynamic>.from(m)))
      .toList();
  return JournalImport(entries, start.clamp(1, kRuleBalances.length));
}

// ---------------------------------------------------------------------------
// Ekspor / Impor EXCEL (.xlsx) — fungsi murni, tanpa I/O (mudah dites)
// ---------------------------------------------------------------------------

/// Nama sheet standar di file ekspor: sheet "Jurnal" berisi tabel trade,
/// sheet "Info" menyimpan "Mulai dari Rule" dkk.
const String kXlsxSheetJurnal = 'Jurnal';
const String kXlsxSheetInfo = 'Info';

const List<String> _kXlsxHeader = [
  'Tanggal', 'Pair', 'Harga Entry', 'Rule Awal', 'Rule Akhir', 'Hasil',
  'Eksekusi', 'Bias', 'SL Murni (Rp)', 'Buangan (Rp)', 'P/L (Rp)',
];

/// Label kolom HASIL: tanda manual (TP/SL); entry tanpa tanda & rules sama
/// tampil 'BE' supaya jelas dibaca di Excel (impor mengembalikan '').
String _hasilLabel(JournalEntry e) {
  if (e.mark.isNotEmpty) return e.mark;
  if (e.taken && e.ruleA == e.ruleB) return 'BE';
  return '';
}

String _p2(int n) => n.toString().padLeft(2, '0'); // 5 → "05" (nama file)

/// Bangun isi file Excel (.xlsx) dari seluruh data jurnal — fungsi murni
/// (tanpa akses file), sehingga roundtrip-nya bisa dites di unit test.
///
/// Sheet "Jurnal": 1 baris header + 1 baris per trade. Tanggal ditulis
/// sebagai sel tanggal-waktu Excel ASLI (bisa difilter/disort di Excel).
/// Kolom SL Murni / Buangan / P/L hanya INFO (dihitung ulang dari rules,
/// diabaikan saat impor) supaya angkanya ikut terlihat saat dibuka.
/// Sheet "Info": metadata, termasuk "Mulai dari Rule" untuk impor.
Uint8List journalExportXlsx(List<JournalEntry> entries, int startRule,
    {DateTime? exportedAt}) {
  final xl = Excel.createExcel();
  final sh = xl[kXlsxSheetJurnal];
  sh.appendRow([for (final h in _kXlsxHeader) TextCellValue(h)]);
  for (int c = 0; c < _kXlsxHeader.length; c++) {
    sh.cell(CellIndex.indexByColumnRow(columnIndex: c, rowIndex: 0)).cellStyle =
        CellStyle(bold: true);
    sh.setColumnWidth(c, c == 0 ? 18 : 13);
  }
  for (final e in entries) {
    sh.appendRow([
      DateTimeCellValue.fromDateTime(e.at),
      TextCellValue(e.pair),
      DoubleCellValue(e.price),
      IntCellValue(e.ruleA),
      IntCellValue(e.ruleB),
      TextCellValue(_hasilLabel(e)),
      TextCellValue(e.taken ? 'KEAMBIL' : 'MISS'),
      TextCellValue(e.bias),
      IntCellValue(e.slMurni),
      IntCellValue(e.buangan),
      IntCellValue(e.pnl),
    ]);
  }
  final info = xl[kXlsxSheetInfo];
  void infoRow(String k, CellValue v) => info.appendRow([TextCellValue(k), v]);
  infoRow('Aplikasi', TextCellValue('transcendence'));
  infoRow('Jenis', TextCellValue('journal'));
  infoRow('Versi', const IntCellValue(1));
  infoRow('Diekspor',
      TextCellValue((exportedAt ?? DateTime.now()).toIso8601String()));
  infoRow('Mulai dari Rule', IntCellValue(startRule));
  infoRow('Jumlah Trade', IntCellValue(entries.length));
  xl.delete('Sheet1'); // buang sheet default — tinggal "Jurnal" & "Info"
  return Uint8List.fromList(xl.encode()!);
}

// ---- pembacaan sel yang toleran: nilai bisa teks / angka / tanggal ----

String? _cellText(Data? d) {
  final v = d?.value;
  if (v == null) return null;
  if (v is TextCellValue) {
    final t = v.value.text;
    return (t == null || t.trim().isEmpty) ? null : t;
  }
  if (v is IntCellValue) return '${v.value}';
  if (v is DoubleCellValue) return '${v.value}';
  if (v is BoolCellValue) return v.value ? 'TRUE' : 'FALSE';
  if (v is DateCellValue || v is DateTimeCellValue) {
    return _cellDate(d)?.toIso8601String();
  }
  return null;
}

int? _cellInt(Data? d) {
  final v = d?.value;
  if (v == null) return null;
  if (v is IntCellValue) return v.value;
  if (v is DoubleCellValue) return v.value.round();
  if (v is TextCellValue) {
    final m = RegExp(r'-?\d+').firstMatch(v.value.text ?? '');
    return m == null ? null : int.tryParse(m.group(0)!);
  }
  return null;
}

double? _cellDouble(Data? d) {
  final v = d?.value;
  if (v == null) return null;
  if (v is IntCellValue) return v.value.toDouble();
  if (v is DoubleCellValue) return v.value;
  if (v is TextCellValue) return numPlain(v.value.text ?? '');
  return null;
}

/// Sel tanggal: DateTimeCellValue/DateCellValue (hasil ekspor app ini),
/// angka serial Excel (epoch 30-12-1899), atau teks (ISO / dd-MM-yyyy).
DateTime? _cellDate(Data? d) {
  final v = d?.value;
  if (v == null) return null;
  if (v is DateTimeCellValue) return v.asDateTimeLocal();
  if (v is DateCellValue) return v.asDateTimeLocal();
  if (v is IntCellValue) return _serialToDate(v.value.toDouble());
  if (v is DoubleCellValue) return _serialToDate(v.value);
  if (v is TextCellValue) return _parseTanggalText(v.value.text ?? '');
  return null;
}

/// Serial Excel → tanggal. Komponen hasil dipakai APA ADANYA (tanpa
/// konversi zona waktu) — persis cara paket excel membaca sel tanggal,
/// sehingga serial dari file ekspor app ini kembali ke nilai yang sama.
DateTime _serialToDate(double serial) {
  final u = DateTime.utc(1899, 12, 30)
      .add(Duration(milliseconds: (serial * 86400000).round()));
  return DateTime(u.year, u.month, u.day, u.hour, u.minute);
}

/// Teks tanggal yang dipahami: ISO 8601, "dd/MM/yyyy HH:mm" / "HH.mm"
/// (gaya app), "dd-MM-yyyy", dan tanggal polosan — Excel lokal Indonesia
/// bisa menyimpan tanggal sebagai teks seperti ini.
DateTime? _parseTanggalText(String raw) {
  final s = raw.trim();
  if (s.isEmpty) return null;
  final iso = DateTime.tryParse(s);
  if (iso != null) return iso;
  final m = RegExp(r'^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})'
          r'(?:[ T,]+(\d{1,2})[:.](\d{2}))?$')
      .firstMatch(s);
  if (m == null) return null;
  return DateTime(
    int.parse(m.group(3)!),
    int.parse(m.group(2)!),
    int.parse(m.group(1)!),
    int.tryParse(m.group(4) ?? '') ?? 0,
    int.tryParse(m.group(5) ?? '') ?? 0,
  );
}

/// Baca file Excel (.xlsx) hasil ekspor jurnal. Kolom dikenali lewat nama
/// header (huruf besar/kecil bebas; baris judul di atas header ditoleransi),
/// baris kosong dilewati. Baris yang tanggal/rules-nya tidak valid melempar
/// FormatException berisi nomor baris — ditangkap UI → toast, tidak crash.
JournalImport journalParseImportXlsx(List<int> bytes) {
  final Excel xl;
  try {
    xl = Excel.decodeBytes(bytes);
  } catch (_) {
    throw const FormatException('file Excel tidak bisa dibaca');
  }

  // "Mulai dari Rule" dibaca dari sheet Info (kalau ada).
  int start = 1;
  for (final name in xl.tables.keys) {
    if (name.trim().toLowerCase() != kXlsxSheetInfo.toLowerCase()) continue;
    for (final row in xl.tables[name]!.rows) {
      final k = row.isNotEmpty ? _cellText(row[0])?.trim().toLowerCase() : null;
      if (k == 'mulai dari rule' && row.length > 1) {
        final v = _cellInt(row[1]);
        if (v != null) start = v;
      }
    }
  }

  // Sheet jurnal: "Jurnal" (tanpa peduli huruf besar/kecil) atau pertama.
  Sheet? sheet;
  for (final name in xl.tables.keys) {
    if (name.trim().toLowerCase() == kXlsxSheetJurnal.toLowerCase()) {
      sheet = xl.tables[name];
      break;
    }
  }
  sheet ??= xl.tables.values.isEmpty ? null : xl.tables.values.first;
  if (sheet == null) throw const FormatException('sheet jurnal tidak ada');

  // Cari baris header (maks 10 baris pertama): wajib ada kolom "tanggal".
  final rows = sheet.rows;
  int headerIdx = -1;
  final col = <String, int>{};
  for (int i = 0; i < rows.length && i < 10; i++) {
    final byName = <String, int>{};
    for (int c = 0; c < rows[i].length; c++) {
      final t = _cellText(rows[i][c])?.trim().toLowerCase();
      if (t != null && t.isNotEmpty && !byName.containsKey(t)) byName[t] = c;
    }
    int? findCol(List<String> names) {
      for (final n in names) {
        final c = byName[n];
        if (c != null) return c;
      }
      for (final e in byName.entries) {
        for (final n in names) {
          if (e.key.contains(n)) return e.value;
        }
      }
      return null;
    }

    final tgl = findCol(['tanggal', 'date', 'waktu']);
    if (tgl == null) continue;
    col['tanggal'] = tgl;
    col['pair'] = findCol(['pair', 'pasangan']) ?? -1;
    col['harga'] = findCol(['harga entry', 'harga', 'price']) ?? -1;
    col['a'] = findCol(['rule awal', 'rules awal']) ?? -1;
    col['b'] = findCol(['rule akhir', 'rules akhir']) ?? -1;
    col['hasil'] = findCol(['hasil']) ?? -1;
    col['eksekusi'] = findCol(['eksekusi']) ?? -1;
    col['bias'] = findCol(['bias']) ?? -1;
    headerIdx = i;
    break;
  }
  if (headerIdx < 0) {
    throw const FormatException('kolom jurnal tidak dikenali');
  }

  final entries = <JournalEntry>[];
  for (int i = headerIdx + 1; i < rows.length; i++) {
    final r = rows[i];
    if (r.every((d) => d?.value == null)) continue; // baris kosong
    Data? cell(String key) {
      final c = col[key] ?? -1;
      return c >= 0 && c < r.length ? r[c] : null;
    }

    final at = _cellDate(cell('tanggal'));
    if (at == null) {
      throw FormatException('baris ${i + 1}: tanggal tidak terbaca');
    }
    final a = _cellInt(cell('a'));
    final b = _cellInt(cell('b'));
    if (a == null ||
        b == null ||
        a < 1 ||
        a > kRuleBalances.length ||
        b < 1 ||
        b > kRuleBalances.length) {
      throw FormatException('baris ${i + 1}: rules awal/akhir tidak valid');
    }
    final pair = (_cellText(cell('pair')) ?? '').trim();
    final hasil = (_cellText(cell('hasil')) ?? '').trim().toUpperCase();
    final ekse = (_cellText(cell('eksekusi')) ?? '').trim().toUpperCase();
    final bias = (_cellText(cell('bias')) ?? '').trim().toUpperCase();
    entries.add(JournalEntry(
      at: at,
      pair: pair.isEmpty ? '-' : pair.toUpperCase(),
      price: _cellDouble(cell('harga')) ?? 0,
      ruleA: a,
      ruleB: b,
      mark: hasil == 'TP' ? 'TP' : (hasil == 'SL' ? 'SL' : ''),
      taken: !ekse.contains('MISS'),
      bias: bias == 'BUY' ? 'BUY' : (bias == 'SELL' ? 'SELL' : ''),
    ));
  }
  return JournalImport(entries, start.clamp(1, kRuleBalances.length));
}

// ---------------------------------------------------------------------------
// Statistik (fungsi murni — mudah dites)
// ---------------------------------------------------------------------------

class Dd {
  final int rp; // total nominal SL murni dalam periode (Rupiah)
  final double pct; // rp terhadap balance rules awal trade SL (≈10% per SL)
  const Dd(this.rp, this.pct);
}

class JournalStats {
  final int total, tp, sl, miss;
  final double winrate; // 0..100 — TP / (TP+SL); miss & BE di luar penyebut
  final int buangan; // total Rp terbuang dari semua SL (turun rules)
  final int startBal, balanceNow;
  final Dd daily, weekly, monthly, all;
  const JournalStats(
      this.total, this.tp, this.sl, this.miss, this.winrate, this.buangan,
      this.startBal, this.balanceNow,
      this.daily, this.weekly, this.monthly, this.all);
}

DateTime _mondayOf(DateTime d) =>
    DateTime(d.year, d.month, d.day).subtract(Duration(days: d.weekday - 1));

/// Drawdown = total nominal SL murni yang terjadi dalam satu periode
/// (harian / mingguan / bulanan / total) — TANPA buangan, sesuai permintaan:
/// SL berdiri sendiri, buangan dilaporkan terpisah di TOTAL BUANGAN.
/// Persen = SL murni terhadap balance rules awal trade (rule A) — tiap SL
/// di tangga rules memang 10% dari balance rule awalnya, jadi % mengikuti
/// balance SAAT trade terjadi, bukan balance awal jurnal.
Dd _ddSum(int slSum, int refBal) =>
    Dd(slSum, slSum > 0 && refBal > 0 ? slSum / refBal * 100 : 0);

/// Hitung semua statistik jurnal. `now` dipisahkan supaya test deterministik.
JournalStats computeJournalStats(
    List<JournalEntry> entries, int startRule, DateTime now) {
  final list = [...entries]..sort((a, b) => a.at.compareTo(b.at));
  int tp = 0, sl = 0, miss = 0, buang = 0;
  for (final e in list) {
    if (e.outcome == 2) {
      miss++; // miss: bukan TP/SL, tidak mengubah balance
      continue;
    }
    if (e.outcome > 0) tp++;
    if (e.outcome < 0) {
      sl++;
      buang += e.buangan;
    }
  }
  final total = list.length;
  // Winrate dari trade yang menghasilkan saja — miss & BE di luar penyebut.
  final decided = tp + sl;
  final winrate = decided == 0 ? 0.0 : tp / decided * 100;
  final startBal = ruleBal(startRule);

  // Hanya trade yang benar-benar diambil yang menggerakkan balance.
  final taken = list.where((e) => e.outcome != 2).toList();
  final balanceNow = taken.isEmpty ? startBal : ruleBal(taken.last.ruleB);

  // Drawdown per periode = akumulasi nominal SL murni (SL berdiri sendiri,
  // tanpa buangan) — yang ditampilkan adalah periode TERBURUK: hari /
  // minggu / bulan dengan penumpukan SL murni terbesar. BUKAN "hari ini"
  // menurut jam HP: trade SL berapa pun tanggalnya langsung terlihat di
  // kartu begitu dicatat (dulu SL bertanggal lampau membuat harian &
  // mingguan selalu Rp0 → terasa tidak berfungsi). Trade TP/BE/miss
  // tidak menyumbang drawdown. TOTAL tetap akumulasi seluruh SL murni.
  // Persen: SL murni periode itu vs jumlah balance rules awal (rule A)
  // trade SL di periode yang sama (tiap SL = 10% balance rule awalnya).
  final byDay = <String, List<int>>{}; // kunci periode → [SL, base]
  final byWeek = <String, List<int>>{};
  final byMonth = <String, List<int>>{};
  void bump(Map<String, List<int>> m, String k, int v, int base) {
    final cur = m[k];
    if (cur == null) {
      m[k] = [v, base];
    } else {
      cur[0] += v;
      cur[1] += base;
    }
  }

  int slAll = 0, baseAll = 0;
  for (final e in taken) {
    final v = e.slMurni;
    if (v == 0) continue;
    final base = ruleBal(e.ruleA);
    slAll += v;
    baseAll += base;
    final t = e.at;
    bump(byDay, '${t.year}-${t.month}-${t.day}', v, base);
    final wk = _mondayOf(t); // pekan Senin–Minggu
    bump(byWeek, '${wk.year}-${wk.month}-${wk.day}', v, base);
    bump(byMonth, '${t.year}-${t.month}', v, base);
  }

  // periode terburuk = penumpukan SL murni terbesar; % memakai base
  // (jumlah balance rule awal) dari SL-SL di periode terburuk itu juga.
  Dd best(Map<String, List<int>> m) {
    List<int>? w;
    for (final cur in m.values) {
      if (w == null || cur[0] > w[0]) w = cur;
    }
    return w == null ? const Dd(0, 0) : _ddSum(w[0], w[1]);
  }

  return JournalStats(total, tp, sl, miss, winrate, buang, startBal,
      balanceNow,
      best(byDay),
      best(byWeek),
      best(byMonth),
      _ddSum(slAll, baseAll));
}

// ---------------------------------------------------------------------------
// Halaman Jurnal
// ---------------------------------------------------------------------------

class JournalPage extends StatefulWidget {
  const JournalPage({super.key});
  @override
  State<JournalPage> createState() => _JournalPageState();
}

class _JournalPageState extends State<JournalPage> {
  List<JournalEntry> _entries = [];
  int _startRule = 1;

  // form
  DateTime _date = DateTime.now();
  TimeOfDay _time = const TimeOfDay(hour: 9, minute: 0);
  final _pair = TextEditingController(text: 'XAUUSD');
  final _price = TextEditingController();
  int? _ruleA;
  int? _ruleB;
  String _mark = 'TP'; // tanda hasil trade: 'TP' | 'SL'
  bool _taken = true; // eksekusi: true = keambil, false = miss
  String _bias = 'BUY'; // bias arah trade: 'BUY' | 'SELL'

  @override
  void initState() {
    super.initState();
    _time = TimeOfDay.now();
    _date = DateTime.now();
    _load();
  }

  @override
  void dispose() {
    _pair.dispose();
    _price.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    final sp = await SharedPreferences.getInstance();
    try {
      final l = jsonDecode(sp.getString('journal_entries') ?? '[]');
      if (l is List) {
        _entries = l
            .whereType<Map>()
            .map((m) => JournalEntry.fromJson(Map<String, dynamic>.from(m)))
            .toList();
      }
    } catch (_) {}
    final sr = sp.getInt('journal_start') ?? 1;
    _startRule = (sr >= 1 && sr <= kRuleBalances.length) ? sr : 1;
    _ruleA ??= _entries.isEmpty ? null : _entries
        .reduce((a, b) => a.at.isAfter(b.at) ? a : b)
        .ruleB;
    if (mounted) setState(() {});
  }

  Future<void> _persist() async {
    final sp = await SharedPreferences.getInstance();
    await sp.setString(
        'journal_entries', jsonEncode(_entries.map((e) => e.toJson()).toList()));
    await sp.setInt('journal_start', _startRule);
  }

  void _toast(String msg, {bool err = false}) {
    final p = Palette.of(context);
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      behavior: SnackBarBehavior.floating,
      backgroundColor: err ? p.redSolid : p.mintSolid,
      content: Text(msg,
          style: TextStyle(
              fontFamily: kBodyFont,
              fontSize: 13,
              fontWeight: FontWeight.w500,
              color: Colors.white)),
    ));
  }

  Future<void> _pickDate() async {
    final d = await showDatePicker(
        context: context,
        initialDate: _date,
        firstDate: DateTime(2020),
        lastDate: DateTime(2100));
    if (d != null) setState(() => _date = d);
  }

  Future<void> _pickTime() async {
    final t = await showTimePicker(context: context, initialTime: _time);
    if (t != null) setState(() => _time = t);
  }

  void _save() {
    final price = numPlain(_price.text);
    if (_ruleA == null || _ruleB == null) {
      _toast('Pilih rules awal & rules akhir dulu.', err: true);
      return;
    }
    if (price == null || price <= 0) {
      _toast('Isi price entry dengan angka yang valid.', err: true);
      return;
    }
    final e = JournalEntry(
      at: DateTime(_date.year, _date.month, _date.day, _time.hour, _time.minute),
      pair: _pair.text.trim().isEmpty ? '-' : _pair.text.trim().toUpperCase(),
      price: price,
      ruleA: _ruleA!,
      ruleB: _ruleB!,
      mark: _mark,
      taken: _taken,
      bias: _bias,
    );
    setState(() {
      _entries.add(e);
      // rantai otomatis: rules awal berikutnya = rules akhir terakhir
      _ruleA = e.ruleB;
      _ruleB = null;
      _price.clear();
      _mark = 'TP';
      _taken = true;
      _bias = 'BUY';
    });
    _persist();
    _toast(
        e.outcome == 2
            ? 'Trade ke-miss tersimpan · tidak dihitung TP/SL.'
            : e.outcome > 0
                ? 'Trade TP tersimpan · +${idr(e.pnl)}'
                : e.outcome < 0
                    ? 'Trade SL tersimpan · buangan ${idr(e.buangan)}'
                    : 'Trade BE tersimpan.',
        err: e.outcome < 0);
  }

  Future<void> _delete(JournalEntry e) async {
    final p = Palette.of(context);
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: p.card,
        shape:
            RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        title: Text('Hapus trade?', style: txt(ctx, 16, w: FontWeight.w600)),
        content: Text(
            '${e.pair}${e.bias.isEmpty ? '' : ' · ${e.bias}'} · '
            '${_fmtDate(e.at)} ${_fmtTime(e.at)}\nRule ${e.ruleA} → ${e.ruleB}',
            style: txt(ctx, 13, color: p.muted)),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: Text('Batal', style: txt(ctx, 13, color: p.muted))),
          TextButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: Text('Hapus',
                  style: txt(ctx, 13,
                      w: FontWeight.w600, color: p.red))),
        ],
      ),
    );
    if (ok == true) {
      setState(() => _entries.remove(e));
      _persist();
    }
  }

  // ---- ekspor / impor -----------------------------------------------------

  void _openExport() {
    final p = Palette.of(context);
    final json = journalExportJson(_entries, _startRule);
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: p.card,
        shape:
            RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        title: Text('Ekspor Data Jurnal',
            style: txt(ctx, 16, w: FontWeight.w600)),
        content: SizedBox(
          width: double.maxFinite,
          child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                    '${_entries.length} trade · mulai dari Rule $_startRule — '
                    'pilih EXCEL untuk file .xlsx (bisa dibuka / diedit di '
                    'Excel / Google Sheets), atau SALIN JSON untuk backup '
                    'penuh & pindah perangkat.',
                    style: txt(ctx, 12.5, color: p.muted, height: 1.4)),
                const SizedBox(height: 10),
                Container(
                  constraints: const BoxConstraints(maxHeight: 220),
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: p.surface1,
                    borderRadius: BorderRadius.circular(9),
                    border: Border.all(width: 1, color: p.line),
                  ),
                  child: SingleChildScrollView(
                    child: SelectableText(json,
                        style: mono(ctx, 10.5, color: p.ink)),
                  ),
                ),
              ]),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: Text('Tutup', style: txt(ctx, 13, color: p.muted))),
          TextButton(
              onPressed: () {
                Navigator.pop(ctx);
                _exportExcel();
              },
              child: Text('Excel',
                  style: txt(ctx, 13, color: p.gold, w: FontWeight.w600))),
          TextButton(
              onPressed: () {
                Clipboard.setData(ClipboardData(text: json));
                Navigator.pop(ctx);
                _toast('Data jurnal disalin ke clipboard.');
              },
              child: Text('Salin',
                  style: txt(ctx, 13, color: p.mint, w: FontWeight.w600))),
        ],
      ),
    );
  }

  Future<void> _openImport() async {
    final p = Palette.of(context);
    // Catatan: controller TIDAK di-dispose manual — dispose saat dialog masih
    // menutup (animasi keluar) memicu "used after disposed" saat rebuild;
    // controller pendek-umur begini cukup dibiarkan di-GC setelah route mati.
    final ctrl = TextEditingController();
    // Hasil dialog: false/null = batal, String = JSON tempelan,
    // Uint8List = isi file Excel yang dipilih.
    final r = await showDialog<dynamic>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: p.card,
        shape:
            RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        title: Text('Impor Data Jurnal',
            style: txt(ctx, 16, w: FontWeight.w600)),
        content: SizedBox(
          width: double.maxFinite,
          child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                    'Pilih file Excel (.xlsx) hasil ekspor, atau tempel teks '
                    'JSON. PERHATIAN: isi jurnal yang sekarang (daftar trade '
                    '& mulai dari rule) akan DIGANTI dengan data yang diimpor.',
                    style: txt(ctx, 12.5, color: p.muted, height: 1.4)),
                const SizedBox(height: 10),
                GhostButton('PILIH FILE EXCEL',
                    key: const Key('jurnal_pick_xlsx'),
                    icon: Icons.grid_on_rounded,
                    expanded: true,
                    onTap: () async {
                  try {
                    final files = await FilePicker.pickFiles(
                        type: FileType.custom,
                        allowedExtensions: ['xlsx']);
                    final f = files.isEmpty ? null : files.single;
                    if (f == null || !ctx.mounted) return;
                    final bytes = await f.readAsBytes();
                    if (bytes.isEmpty) return;
                    if (ctx.mounted) Navigator.pop(ctx, bytes);
                  } catch (_) {
                    _toast('Pemilih file tidak tersedia di perangkat ini.',
                        err: true);
                  }
                }),
                const SizedBox(height: 10),
                Center(
                    child: Text('— atau tempel JSON —',
                        style: txt(ctx, 11, color: p.dim))),
                const SizedBox(height: 8),
                TextField(
                  key: const Key('jurnal_import_text'),
                  controller: ctrl,
                  maxLines: 5,
                  style: mono(ctx, 11.5, color: p.ink),
                  decoration: InputDecoration(
                    hintText: '{"app":"transcendence","type":"journal",...}',
                    hintStyle: txt(ctx, 11, color: p.dim),
                    filled: true,
                    fillColor: p.surface1,
                    contentPadding: const EdgeInsets.all(10),
                    enabledBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(9),
                        borderSide: BorderSide(color: p.line)),
                    focusedBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(9),
                        borderSide: BorderSide(color: p.gold)),
                  ),
                ),
              ]),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: Text('Batal', style: txt(ctx, 13, color: p.muted))),
          TextButton(
              onPressed: () => Navigator.pop(ctx, ctrl.text.trim()),
              child: Text('Impor',
                  style: txt(ctx, 13, color: p.mint, w: FontWeight.w600))),
        ],
      ),
    );
    if (r == null || r == false) return;
    try {
      final imp = r is String
          ? journalParseImport(r)
          : journalParseImportXlsx(r as List<int>);
      setState(() {
        _entries = imp.entries;
        _startRule = imp.startRule;
        // rantai otomatis: rules awal berikutnya = rules akhir terakhir
        _ruleA = _entries.isEmpty
            ? null
            : _entries.reduce((a, b) => a.at.isAfter(b.at) ? a : b).ruleB;
        _ruleB = null;
      });
      _persist();
      _toast('${imp.entries.length} trade diimpor — data jurnal diganti.');
    } on FormatException catch (e) {
      _toast('Impor gagal: ${e.message}', err: true);
    } catch (_) {
      _toast('Format tidak dikenali — pilih file Excel hasil ekspor '
          'atau tempel JSON.',
          err: true);
    }
  }

  /// Ekspor ke file Excel (.xlsx): tulis ke folder temp lalu dibagikan
  /// lewat share sheet (simpan ke Files/Drive, kirim via WA, dll).
  Future<void> _exportExcel() async {
    try {
      final bytes = journalExportXlsx(_entries, _startRule);
      final dir = await getTemporaryDirectory();
      final t = DateTime.now();
      final name = 'jurnal-transcendence-'
          '${t.year}${_p2(t.month)}${_p2(t.day)}-'
          '${_p2(t.hour)}${_p2(t.minute)}.xlsx';
      final f = File('${dir.path}/$name');
      await f.writeAsBytes(bytes, flush: true);
      await SharePlus.instance.share(ShareParams(
        files: [XFile(f.path)],
        fileNameOverrides: [name],
        title: name,
        subject: 'Backup jurnal Transcendence (Excel)',
      ));
    } catch (_) {
      _toast('Gagal membuat file Excel.', err: true);
    }
  }

  String _fmtDate(DateTime d) =>
      '${d.day.toString().padLeft(2, '0')}/${d.month.toString().padLeft(2, '0')}/${d.year}';
  String _fmtTime(DateTime d) =>
      '${d.hour.toString().padLeft(2, '0')}.${d.minute.toString().padLeft(2, '0')}';

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final s = computeJournalStats(_entries, _startRule, DateTime.now());
    final netPnl = s.balanceNow - s.startBal;

    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionHead(Icons.history_edu, 'Trading Journal'),
          const SizedBox(height: 4),
          Text('Catat tiap trade, tandai hasilnya TP/SL dan apakah keambil '
              'atau miss — statistik & drawdown terhitung otomatis.',
              style: txt(context, 12, color: p.muted)),
          const SizedBox(height: 14),

          // ---- TOTAL BUANGAN (di atas, sesuai permintaan) ----
          _buanganCard(p, s),
          const SizedBox(height: 10),

          // ---- balance sekarang + mulai dari rule ----
          _balanceCard(p, s, netPnl),
          const SizedBox(height: 10),

          // ---- total trade / tp / sl / winrate ----
          Row(children: [
            _mini(p, 'TOTAL', '${s.total}', p.ink),
            const SizedBox(width: 8),
            _mini(p, 'TP', '${s.tp}', p.mint),
            const SizedBox(width: 8),
            _mini(p, 'SL', '${s.sl}', p.red),
            const SizedBox(width: 8),
            _mini(p, 'WINRATE',
                '${s.winrate.toStringAsFixed(1).replaceFirst('.', ',')}%', p.gold),
          ]),
          const SizedBox(height: 6),
          Row(children: [
            // ringkasan eksekusi: keambil vs miss (permintaan user)
            _mini(p, 'KEAMBIL', '${s.total - s.miss}', p.mint),
            const SizedBox(width: 8),
            _mini(p, 'MISS', '${s.miss}', p.dim),
          ]),
          if (s.miss > 0) ...[
            const SizedBox(height: 6),
            Text('${s.miss} trade di-miss · tidak dihitung TP/SL & balance',
                style: txt(context, 11, color: p.dim)),
          ],
          const SizedBox(height: 10),

          // ---- drawdown ----
          _ddCard(p, s),
          const SizedBox(height: 16),

          // ---- form catat trade ----
          _formCard(p),
          const SizedBox(height: 16),

          // ---- riwayat ----
          SectionHead(Icons.receipt_long, 'Riwayat Trade',
              trailing: BadgePill('${_entries.length} trade',
                  color: p.warm, bg: p.cardHi)),
          const SizedBox(height: 10),
          Row(children: [
            Expanded(
              child: GhostButton('EKSPOR',
                  icon: Icons.ios_share,
                  expanded: true,
                  onTap: _openExport),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: GhostButton('IMPOR',
                  icon: Icons.save_alt, expanded: true, onTap: _openImport),
            ),
          ]),
          const SizedBox(height: 10),
          if (_entries.isEmpty)
            ObsCard(
              color: p.surface1,
              children: [
                Text('Belum ada trade. Catat trade pertamamu di form atas — '
                    'tandai hasilnya TP/SL dan apakah keambil atau miss.',
                    style: txt(context, 12.5, color: p.muted, height: 1.5)),
              ],
            )
          else
            ..._listCards(p, s),
        ],
      ),
    );
  }

  // ---- kartu-kartu ---------------------------------------------------------

  Widget _buanganCard(Palette p, JournalStats s) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: p.card,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(width: 1, color: p.redsoft),
      ),
      child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
        Row(children: [
          Icon(Icons.output_rounded, size: 15, color: p.red),
          const SizedBox(width: 6),
          Expanded(
            child: Text('TOTAL BUANGAN',
                maxLines: 1, overflow: TextOverflow.ellipsis,
                style: labelSm(context, color: p.muted)),
          ),
        ]),
        const SizedBox(height: 6),
        Text(idr(s.buangan),
            style: mono(context, 22, w: FontWeight.w600, color: p.red)),
        const SizedBox(height: 4),
        Text('Akumulasi balance terbuang dari trade yang turun rules — '
            'di luar nominal SL.',
            style: txt(context, 10.5, color: p.dim, height: 1.35)),
      ]),
    );
  }

  Widget _balanceCard(Palette p, JournalStats s, int netPnl) {
    return ObsCard(
      children: [
        Row(children: [
          Expanded(
            child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
              Text('BALANCE SEKARANG', style: labelSm(context, color: p.muted)),
              const SizedBox(height: 5),
              Text(idr(s.balanceNow),
                  style: mono(context, 22, w: FontWeight.w600, color: p.gold)),
            ]),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
            decoration: BoxDecoration(
              color: netPnl >= 0 ? p.goodsoft : p.redsoft,
              borderRadius: BorderRadius.circular(8),
            ),
            child: Text(
                '${netPnl >= 0 ? '+' : '−'}${idr(netPnl.abs()).substring(2)}',
                style: mono(context, 12.5,
                    w: FontWeight.w600,
                    color: netPnl >= 0 ? p.mint : p.red)),
          ),
        ]),
        const SizedBox(height: 12),
        Row(children: [
          Text('MULAI DARI RULE', style: labelSm(context, color: p.dim)),
          const SizedBox(width: 10),
          Expanded(
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 10),
              decoration: BoxDecoration(
                color: p.surface1,
                borderRadius: BorderRadius.circular(9),
                border: Border.all(width: 1, color: p.line),
              ),
              child: DropdownButtonHideUnderline(
                child: DropdownButton<int>(
                  value: _startRule,
                  isExpanded: true,
                  dropdownColor: p.card,
                  borderRadius: BorderRadius.circular(10),
                  icon: Icon(Icons.expand_more,
                      size: 17, color: p.muted),
                  style: mono(context, 12.5, w: FontWeight.w600),
                  items: [
                    for (int n = 1; n <= kRuleBalances.length; n++)
                      DropdownMenuItem(
                          value: n,
                          child: FittedBox(
                              fit: BoxFit.scaleDown,
                              alignment: Alignment.centerLeft,
                              // Nominal PERSIS — sama dgn dropdown rules.
                              child: Text('Rule $n · ${idr(ruleBal(n))}'))),
                  ],
                  onChanged: (v) {
                    if (v == null) return;
                    setState(() => _startRule = v);
                    _persist();
                  },
                ),
              ),
            ),
          ),
        ]),
      ],
    );
  }

  Widget _mini(Palette p, String label, String val, Color c) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 8),
        decoration: BoxDecoration(
          color: p.card,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(width: 1, color: p.lineSoft),
        ),
        child: Column(children: [
          Text(label,
              maxLines: 1, overflow: TextOverflow.ellipsis,
              style: labelSm(context, color: p.dim)),
          const SizedBox(height: 4),
          Text(val,
              maxLines: 1, overflow: TextOverflow.ellipsis,
              style: mono(context, 15, w: FontWeight.w600, color: c)),
        ]),
      ),
    );
  }

  Widget _ddCard(Palette p, JournalStats s) {
    Widget cell(String label, Dd d, Color c) => Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(label, style: labelSm(context, color: p.dim)),
            const SizedBox(height: 4),
            Text(shortIdr(d.rp.toDouble()),
                style: mono(context, 14, w: FontWeight.w600, color: c)),
            const SizedBox(height: 2),
            Text('${d.pct.toStringAsFixed(1).replaceFirst('.', ',')}%',
                style: mono(context, 10.5, color: p.muted)),
          ]),
        );
    return ObsCard(
      children: [
        Row(children: [
          Icon(Icons.waterfall_chart, size: 15, color: p.gold),
          const SizedBox(width: 7),
          Text('DRAWDOWN', style: labelSm(context, color: p.muted)),
        ]),
        const SizedBox(height: 12),
        Row(children: [
          cell('HARIAN', s.daily, p.red),
          const SizedBox(width: 12),
          cell('MINGGUAN', s.weekly, p.red),
        ]),
        const SizedBox(height: 12),
        Row(children: [
          cell('BULANAN', s.monthly, p.red),
          const SizedBox(width: 12),
          cell('TOTAL', s.all, p.red),
        ]),
        const SizedBox(height: 10),
        Text('Nominal SL murni pada periode TERBURUK — hari / minggu / '
            'bulan dengan penumpukan SL terbesar (% dari balance rules '
            'saat trade). Trade SL berapa pun tanggalnya langsung terlihat '
            'di sini. Buangan tidak ikut dihitung (lihat TOTAL BUANGAN).',
            style: txt(context, 10.5, color: p.dim, height: 1.35)),
      ],
    );
  }

  Widget _formCard(Palette p) {
    final preview = (_ruleA != null && _ruleB != null)
        ? JournalEntry(
            at: DateTime.now(),
            pair: _pair.text,
            price: 0,
            ruleA: _ruleA!,
            ruleB: _ruleB!,
            mark: _mark,
            taken: _taken,
            bias: _bias)
        : null;

    return ObsCard(
      children: [
        SectionHead(Icons.edit_note, 'Catat Trade Baru'),
        const SizedBox(height: 12),
        Row(children: [
          Expanded(
            child: GhostButton(_fmtDate(_date),
                icon: Icons.calendar_today_outlined,
                expanded: true,
                onTap: _pickDate),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: GhostButton(
                _fmtTime(DateTime(0, 1, 1, _time.hour, _time.minute)),
                icon: Icons.schedule_outlined,
                expanded: true,
                onTap: _pickTime),
          ),
        ]),
        const SizedBox(height: 12),
        _pairField(p),
        const SizedBox(height: 12),
        LabeledField(
          key: const Key('jurnal_price'),
          label: 'PRICE ENTRY',
          controller: _price,
          onChanged: (_) {},
          prefix: '\$',
          hint: 'Contoh: 4021,5',
        ),
        const SizedBox(height: 12),
        Row(children: [
          Expanded(child: _ruleDropdown(p, true)),
          const SizedBox(width: 8),
          Icon(Icons.east, size: 16, color: p.muted),
          const SizedBox(width: 8),
          Expanded(child: _ruleDropdown(p, false)),
        ]),
        const SizedBox(height: 12),
        // ---- eksekusi (keambil/miss) + tanda hasil (TP/SL) ----
        Row(children: [
          Expanded(
            child: _seg(p, 'EKSEKUSI', [('KEAMBIL', _taken), ('MISS', !_taken)],
                _taken ? p.goldSolid : p.redSolid,
                (v) => setState(() => _taken = v == 'KEAMBIL')),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: AbsorbPointer(
              absorbing: !_taken,
              child: Opacity(
                opacity: _taken ? 1 : 0.4,
                child: _seg(p, 'HASIL TRADE',
                    [('TP', _mark == 'TP'), ('SL', _mark == 'SL')],
                    _mark == 'TP' ? p.mintSolid : p.redSolid,
                    (v) => setState(() => _mark = v)),
              ),
            ),
          ),
        ]),
        const SizedBox(height: 12),
        // ---- bias arah trade: BUY / SELL ----
        _seg(p, 'BIAS', [('BUY', _bias == 'BUY'), ('SELL', _bias == 'SELL')],
            _bias == 'BUY' ? p.mintSolid : p.redSolid,
            (v) => setState(() => _bias = v)),
        if (preview != null) ...[
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
            decoration: BoxDecoration(
              color: p.surface1,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(width: 1, color: p.line),
            ),
            child: Row(children: [
              _outcomeBadge(p, preview.outcome),
              const SizedBox(width: 6),
              _biasBadge(p, preview.bias),
              const Spacer(),
              if (preview.outcome == 2)
                Text('tidak dihitung',
                    style: txt(context, 11.5, color: p.muted))
              else
                Text(
                  preview.outcome > 0
                      ? '+${idr(preview.pnl)}'
                      : preview.outcome < 0
                          ? '−${idr(preview.pnl.abs())}'
                          : 'Rp0',
                  style: mono(context, 13.5,
                      w: FontWeight.w600,
                      color: preview.outcome > 0
                          ? p.mint
                          : preview.outcome < 0
                              ? p.red
                              : p.muted),
                ),
            ]),
          ),
        ],
        const SizedBox(height: 12),
        GoldButton('Simpan Trade', icon: Icons.save_outlined, onTap: _save),
      ],
    );
  }

  Widget _pairField(Palette p) {
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('PAIR', style: labelSm(context)),
      const SizedBox(height: 6),
      Container(
        decoration: BoxDecoration(
          color: p.surface1,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(width: 1, color: p.line),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: TextField(
          controller: _pair,
          textCapitalization: TextCapitalization.characters,
          style: mono(context, 15, w: FontWeight.w600),
          decoration: const InputDecoration(
              isDense: true,
              filled: false,
              border: InputBorder.none,
              contentPadding: EdgeInsets.zero),
          cursorColor: p.goldSolid,
        ),
      ),
    ]);
  }

  Widget _ruleDropdown(Palette p, bool isA) {
    final label = isA ? 'RULES AWAL' : 'RULES AKHIR';
    final v = isA ? _ruleA : _ruleB;
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text(label, style: labelSm(context)),
      const SizedBox(height: 6),
      Container(
        key: Key(isA ? 'jurnal_ruleA' : 'jurnal_ruleB'),
        padding: const EdgeInsets.symmetric(horizontal: 10),
        decoration: BoxDecoration(
          color: p.surface1,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(width: 1, color: p.line),
        ),
        child: DropdownButtonHideUnderline(
          child: DropdownButton<int>(
            value: v,
            isExpanded: true,
            dropdownColor: p.card,
            borderRadius: BorderRadius.circular(10),
            icon: Icon(Icons.expand_more, size: 17, color: p.muted),
            hint: Text('Rule…',
                style: mono(context, 12.5, color: p.dim)),
            style: mono(context, 12.5, w: FontWeight.w600),
            items: [
              for (int n = 1; n <= kRuleBalances.length; n++)
                DropdownMenuItem(
                    value: n,
                    child: FittedBox(
                        fit: BoxFit.scaleDown,
                        alignment: Alignment.centerLeft,
                        // Nominal PERSIS (bukan format singkat Rp346rb yang
                        // membulatkan 345.600 → terlihat kelebihan ~1k).
                        child: Text('Rule $n · ${idr(ruleBal(n))}'))),
            ],
            onChanged: (val) {
              setState(() {
                if (isA) {
                  _ruleA = val;
                  if (_ruleB != null && _ruleB! < val!) _ruleB = val;
                } else {
                  _ruleB = val;
                }
                _syncMark();
              });
            },
          ),
        ),
      ),
    ]);
  }

  /// Tanda hasil mengikuti arah rules secara otomatis — turun = SL,
  /// naik = TP. Rules sama: tanda terakhir dipertahankan (bisa dipakai
  /// untuk "SL kecil yang tidak pindah rule").
  void _syncMark() {
    if (_ruleA == null || _ruleB == null) return;
    if (_ruleB! < _ruleA!) {
      _mark = 'SL';
    } else if (_ruleB! > _ruleA!) {
      _mark = 'TP';
    }
  }

  Widget _seg(Palette p, String label, List<(String, bool)> opts, Color cAct,
      void Function(String) onSel) {
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text(label, style: labelSm(context, color: p.muted)),
      const SizedBox(height: 6),
      Container(
        padding: const EdgeInsets.all(3),
        decoration: BoxDecoration(
          color: p.surface1,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(width: 1, color: p.line),
        ),
        child: Row(
          children: [
            for (final (name, active) in opts)
              Expanded(
                child: GestureDetector(
                  onTap: () => onSel(name),
                  behavior: HitTestBehavior.opaque,
                  child: Container(
                    alignment: Alignment.center,
                    padding: const EdgeInsets.symmetric(vertical: 7),
                    decoration: BoxDecoration(
                      color: active ? cAct : Colors.transparent,
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Text(name,
                        maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: mono(context, 11.5, w: FontWeight.w700,
                            color: active ? Colors.white : p.muted)),
                  ),
                ),
              ),
          ],
        ),
      ),
    ]);
  }

  Widget _outcomeBadge(Palette p, int outcome) {
    final (txt2, c, bg) = outcome == 2
        ? ('MISS', p.warm, p.cardHi)
        : outcome > 0
            ? ('TP', p.mint, p.goodsoft)
            : outcome < 0
                ? ('SL', p.red, p.redsoft)
                : ('BE', p.muted, p.cardHi);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
      decoration: BoxDecoration(
          color: bg, borderRadius: BorderRadius.circular(7)),
      child: Text(txt2,
          style: mono(context, 11.5, w: FontWeight.w700, color: c)),
    );
  }

  /// Pil arah trade: BUY (hijau) / SELL (merah). Entry lama tanpa bias
  /// tidak menampilkan apa-apa.
  Widget _biasBadge(Palette p, String bias) {
    if (bias.isEmpty) return const SizedBox.shrink();
    final buy = bias == 'BUY';
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
      decoration: BoxDecoration(
          color: buy ? p.goodsoft : p.redsoft,
          borderRadius: BorderRadius.circular(7)),
      child: Text(bias,
          style: mono(context, 11.5, w: FontWeight.w700,
              color: buy ? p.mint : p.red)),
    );
  }

  List<Widget> _listCards(Palette p, JournalStats s) {
    final sorted = [..._entries]..sort((a, b) => b.at.compareTo(a.at));
    return [
      for (final e in sorted)
        Container(
          margin: const EdgeInsets.only(bottom: 8),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: p.card,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(width: 1, color: p.lineSoft),
          ),
          child: Row(children: [
            Expanded(
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                Row(children: [
                  Flexible(
                    child: Text('${_fmtDate(e.at)}  ${_fmtTime(e.at)}',
                        maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: mono(context, 11.5, color: p.muted)),
                  ),
                  const SizedBox(width: 8),
                  Flexible(child: BadgePill(e.pair, color: p.gold, bg: p.cardHi)),
                  if (e.bias.isNotEmpty) ...[
                    const SizedBox(width: 6),
                    Flexible(child: _biasBadge(p, e.bias)),
                  ],
                ]),
                const SizedBox(height: 5),
                Row(children: [
                  Flexible(
                    child: Text('Rule ${e.ruleA}',
                        maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: mono(context, 13, w: FontWeight.w600)),
                  ),
                  Icon(Icons.east, size: 13, color: p.dim),
                  Flexible(
                    child: Text('Rule ${e.ruleB}',
                        maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: mono(context, 13, w: FontWeight.w600)),
                  ),
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text('\$${e.price.toStringAsFixed(2)}',
                        maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: mono(context, 11.5, color: p.dim)),
                  ),
                ]),
              ]),
            ),
            const SizedBox(width: 8),
            Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
              _outcomeBadge(p, e.outcome),
              const SizedBox(height: 4),
              Text(
                e.outcome == 2
                    ? 'miss'
                    : e.outcome > 0
                        ? '+${idr(e.pnl)}'
                        : e.outcome < 0
                            ? '−${idr(e.pnl.abs())}'
                            : 'Rp0',
                style: mono(context, 12.5,
                    w: FontWeight.w600,
                    color: e.outcome == 2
                        ? p.dim
                        : e.outcome > 0
                            ? p.mint
                            : e.outcome < 0
                                ? p.red
                                : p.muted),
              ),
            ]),
            IconButton(
                visualDensity: VisualDensity.compact,
                onPressed: () => _delete(e),
                icon: Icon(Icons.delete_outline, size: 18, color: p.dim)),
          ]),
        ),
    ];
  }
}
