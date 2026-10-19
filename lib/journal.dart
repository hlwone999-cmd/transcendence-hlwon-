// Transcendence — journal.dart
// FITUR BARU: Jurnal Trade.
//
// Catat trade: tanggal + jam, pair, price entry, rules awal → rules akhir.
// Hasil diturunkan otomatis dari tangga rules (engine.dart):
//   - rules naik (akhir > awal)  = TP  (+ selisih balance antar rule)
//   - rules turun (akhir < awal) = SL  → "buangan" = selisih balance rule
//   - sama                       = BE  (0)
// Statistik: total trade, total TP, total SL, winrate, TOTAL BUANGAN,
// balance sekarang (berantai dari "Mulai dari Rule"), dan drawdown
// harian / mingguan / bulanan / all (peak-to-trough di atas kurva balance).
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
  const JournalEntry({
    required this.at,
    required this.pair,
    required this.price,
    required this.ruleA,
    required this.ruleB,
  });

  Map<String, dynamic> toJson() => {
        'at': at.toIso8601String(),
        'pair': pair,
        'price': price,
        'a': ruleA,
        'b': ruleB,
      };

  static JournalEntry fromJson(Map<String, dynamic> j) => JournalEntry(
        at: DateTime.tryParse('${j['at']}') ?? DateTime.now(),
        pair: '${j['pair'] ?? '-'}',
        price: (j['price'] as num?)?.toDouble() ?? 0,
        ruleA: (j['a'] as num?)?.toInt() ?? 1,
        ruleB: (j['b'] as num?)?.toInt() ?? 1,
      );

  /// 1 = TP (naik rules), -1 = SL (turun rules), 0 = BE (sama).
  int get outcome => ruleB > ruleA ? 1 : (ruleB < ruleA ? -1 : 0);

  /// P/L sesuai tangga rules: balance rule akhir − balance rule awal.
  int get pnl => ruleBal(ruleB) - ruleBal(ruleA);

  /// Buangan saat SL: berapa balance "terbuang" karena turun rules.
  int get buangan => outcome < 0 ? ruleBal(ruleA) - ruleBal(ruleB) : 0;
}

// ---------------------------------------------------------------------------
// Statistik (fungsi murni — mudah dites)
// ---------------------------------------------------------------------------

class Dd {
  final int rp; // drawdown dalam Rupiah
  final double pct; // drawdown relatif thd puncaknya (%)
  const Dd(this.rp, this.pct);
}

class JournalStats {
  final int total, tp, sl;
  final double winrate; // 0..100
  final int buangan; // total Rp terbuang dari semua SL
  final int startBal, balanceNow;
  final Dd daily, weekly, monthly, all;
  const JournalStats(
      this.total, this.tp, this.sl, this.winrate, this.buangan,
      this.startBal, this.balanceNow,
      this.daily, this.weekly, this.monthly, this.all);
}

DateTime _mondayOf(DateTime d) =>
    DateTime(d.year, d.month, d.day).subtract(Duration(days: d.weekday - 1));

Dd _ddOf(List<(DateTime, int)> pts, bool Function(DateTime) inPeriod) {
  int peak = 0, worst = 0, worstPeak = 0;
  bool has = false;
  for (final (t, b) in pts) {
    if (!inPeriod(t)) continue;
    if (!has || b > peak) {
      peak = b;
      has = true;
    }
    final d = peak - b;
    if (d > worst) {
      worst = d;
      worstPeak = peak;
    }
  }
  return Dd(worst, worst > 0 && worstPeak > 0 ? worst / worstPeak * 100 : 0);
}

Dd _ddAll(List<(DateTime, int)> pts) {
  int peak = 0, worst = 0, worstPeak = 0;
  for (final (_, b) in pts) {
    if (b > peak) peak = b;
    final d = peak - b;
    if (d > worst) {
      worst = d;
      worstPeak = peak;
    }
  }
  return Dd(worst, worst > 0 && worstPeak > 0 ? worst / worstPeak * 100 : 0);
}

/// Hitung semua statistik jurnal. `now` dipisahkan supaya test deterministik.
JournalStats computeJournalStats(
    List<JournalEntry> entries, int startRule, DateTime now) {
  final list = [...entries]..sort((a, b) => a.at.compareTo(b.at));
  int tp = 0, sl = 0, buang = 0;
  for (final e in list) {
    if (e.outcome > 0) tp++;
    if (e.outcome < 0) {
      sl++;
      buang += e.buangan;
    }
  }
  final total = list.length;
  final winrate = total == 0 ? 0.0 : tp / total * 100;
  final startBal = ruleBal(startRule);
  final balanceNow = list.isEmpty ? startBal : ruleBal(list.last.ruleB);

  // Kurva balance kronologis: titik awal (anchor) + hasil tiap trade.
  final pts = <(DateTime, int)>[];
  if (list.isNotEmpty) pts.add((list.first.at, startBal));
  for (final e in list) {
    pts.add((e.at, ruleBal(e.ruleB)));
  }

  bool sameDay(DateTime t) =>
      t.year == now.year && t.month == now.month && t.day == now.day;
  bool sameWeek(DateTime t) => _mondayOf(t) == _mondayOf(now);
  bool sameMonth(DateTime t) => t.year == now.year && t.month == now.month;

  return JournalStats(total, tp, sl, winrate, buang, startBal, balanceNow,
      _ddOf(pts, sameDay), _ddOf(pts, sameWeek), _ddOf(pts, sameMonth),
      _ddAll(pts));
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
    );
    setState(() {
      _entries.add(e);
      // rantai otomatis: rules awal berikutnya = rules akhir terakhir
      _ruleA = e.ruleB;
      _ruleB = null;
      _price.clear();
    });
    _persist();
    _toast(
        e.outcome > 0
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
            '${e.pair} · ${_fmtDate(e.at)} ${_fmtTime(e.at)}\nRule ${e.ruleA} → ${e.ruleB}',
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
          Text('Catat tiap trade — statistik & drawdown terhitung otomatis '
              'dari tangga rules.',
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
                    'hasil TP/SL otomatis dari rules awal → akhir.',
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
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: p.card,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(width: 1, color: p.redsoft),
      ),
      child: Row(children: [
        Expanded(
          child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
            Row(children: [
              Icon(Icons.output_rounded, size: 15, color: p.red),
              const SizedBox(width: 6),
              Expanded(
                child: Text('TOTAL BUANGAN (SL TURUN RULES)',
                    maxLines: 1, overflow: TextOverflow.ellipsis,
                    style: labelSm(context, color: p.muted)),
              ),
            ]),
            const SizedBox(height: 6),
            Text(idr(s.buangan),
                style: mono(context, 22, w: FontWeight.w600, color: p.red)),
          ]),
        ),
        const SizedBox(width: 10),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
          decoration: BoxDecoration(
              color: p.redsoft, borderRadius: BorderRadius.circular(8)),
          child: Text('${s.sl}× SL',
              style: mono(context, 12, w: FontWeight.w600, color: p.red)),
        ),
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
                          child: Text(
                              'Rule $n · ${shortIdr(ruleBal(n).toDouble())}'))
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
          cell('ALL (MAKS)', s.all, p.red),
        ]),
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
            ruleB: _ruleB!)
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
              const Spacer(),
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
                    child: Text('Rule $n · ${shortIdr(ruleBal(n).toDouble())}'))
            ],
            onChanged: (val) {
              setState(() {
                if (isA) {
                  _ruleA = val;
                  if (_ruleB != null && _ruleB! < val!) _ruleB = val;
                } else {
                  _ruleB = val;
                }
              });
            },
          ),
        ),
      ),
    ]);
  }

  Widget _outcomeBadge(Palette p, int outcome) {
    final (txt2, c, bg) = outcome > 0
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
                e.outcome > 0
                    ? '+${idr(e.pnl)}'
                    : e.outcome < 0
                        ? '−${idr(e.pnl.abs())}'
                        : 'Rp0',
                style: mono(context, 12.5,
                    w: FontWeight.w600,
                    color: e.outcome > 0
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
