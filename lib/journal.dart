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
// harian / mingguan / bulanan / total = akumulasi nominal SL MURNI per
// periode (10% dari balance rule awal — TANPA buangan, SL berdiri
// sendiri; buangan dilaporkan terpisah di kartu TOTAL BUANGAN).
//
// Modul ini STANDALONE: tidak mengubah algoritma/kalkulator existing.
// Data disimpan di SharedPreferences (journal_entries & journal_start).
import 'dart:convert';

import 'package:flutter/material.dart';
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

  bool sameDay(DateTime t) =>
      t.year == now.year && t.month == now.month && t.day == now.day;
  bool sameWeek(DateTime t) => _mondayOf(t) == _mondayOf(now);
  bool sameMonth(DateTime t) => t.year == now.year && t.month == now.month;

  // Drawdown = akumulasi nominal SL murni per periode (SL berdiri sendiri,
  // tanpa buangan). Trade TP/BE/miss tidak menyumbang drawdown — begitu
  // trade SL dicatat, semua periode yang memuat tanggalnya langsung terisi.
  // Persen: tiap SL murni = 10% dari balance rule awalnya, jadi referensi
  // % = jumlah balance rules awal (rule A) dari trade SL di periode itu.
  int slDay = 0, slWeek = 0, slMonth = 0, slAll = 0;
  int baseDay = 0, baseWeek = 0, baseMonth = 0, baseAll = 0;
  for (final e in taken) {
    final v = e.slMurni;
    if (v == 0) continue;
    final base = ruleBal(e.ruleA);
    slAll += v;
    baseAll += base;
    if (sameDay(e.at)) {
      slDay += v;
      baseDay += base;
    }
    if (sameWeek(e.at)) {
      slWeek += v;
      baseWeek += base;
    }
    if (sameMonth(e.at)) {
      slMonth += v;
      baseMonth += base;
    }
  }

  return JournalStats(total, tp, sl, miss, winrate, buang, startBal,
      balanceNow,
      _ddSum(slDay, baseDay),
      _ddSum(slWeek, baseWeek),
      _ddSum(slMonth, baseMonth),
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
        Text('Nominal SL murni per periode (% dari balance rules saat '
            'trade) — buangan tidak ikut dihitung di sini (lihat TOTAL '
            'BUANGAN).',
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
