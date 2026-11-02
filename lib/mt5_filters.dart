// Transcendence — mt5_filters.dart
// Filter tanggal MT5 (Phase 1B): preset rolling, bulan kalender, custom
// range, dan zona waktu tampilan.
//
// SEMANTIK INTERVAL (harus cocok dengan backend — lihat
// docs/DATE-TIMEZONE-RECONCILIATION.md):
// - Semua perbandingan backend memakai UTC (zona kanonik penyimpanan).
// - Klien mengonversi batas tanggal zona tampilan -> UTC SEKALI di sini,
//   lalu mengirim `from` (inklusif), `to` (EKSKLUSIF, `to_exclusive=1`).
//   Interval efektif: [from, to) — trade tepat tengah malam tidak dobel
//   dan tidak terbuang antar periode berurutan.
// - Rolling N hari  : [now - N hari, now) — durasi bergerak, bukan kalender.
// - Bulan ini/lalu  : batas kalender di zona tampilan (BUKAN rolling 30
//   hari), dikonversi ke UTC.
// - Custom          : [mulai 00:00 zona, akhir hari +1 00:00 zona).
//
// Zona waktu: hanya zona TANPA DST (UTC, Asia/Jakarta) di fase ini.
// Offset disimpan eksplisit; menambah IANA lain (dengan DST) nantinya
// butuh package `timezone` — struktur AppTimeZone sudah menyiapkan itu.

enum DatePreset { d7, d30, d90, thisMonth, lastMonth, all, custom }

extension DatePresetX on DatePreset {
  String get label => switch (this) {
    DatePreset.d7 => '7 HARI',
    DatePreset.d30 => '30 HARI',
    DatePreset.d90 => '90 HARI',
    DatePreset.thisMonth => 'BLN INI',
    DatePreset.lastMonth => 'BLN LALU',
    DatePreset.all => 'SEMUA',
    DatePreset.custom => 'PILIH TGL',
  };
}

class AppTimeZone {
  final String id; // IANA id
  final String label; // tampilan singkat
  final int offsetMinutes; // offset UTC, zona tanpa DST
  const AppTimeZone(this.id, this.label, this.offsetMinutes);

  static const utc = AppTimeZone('UTC', 'UTC', 0);
  static const jakarta = AppTimeZone('Asia/Jakarta', 'WIB · Jakarta', 420);
  static const List<AppTimeZone> supported = [jakarta, utc];

  @override
  String toString() => label;

  /// Konversi dinding-waktu di zona ini -> titik UTC yang sesungguhnya.
  DateTime localToUtc(DateTime localWallClock) =>
      localWallClock.subtract(Duration(minutes: offsetMinutes));

  /// Titik UTC -> dinding-waktu di zona ini (untuk tampilan).
  DateTime utcToLocal(DateTime utcMoment) =>
      utcMoment.add(Duration(minutes: offsetMinutes));
}

class Mt5DateRange {
  final String? fromUtc; // inklusif
  final String? toUtc; // EKSKLUSIF (to_exclusive=1) kecuali null
  final String label; // deskripsi interval utk UI (zona tampilan)

  const Mt5DateRange(this.fromUtc, this.toUtc, this.label);
}

const _bulan = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'Mei',
  'Jun',
  'Jul',
  'Agu',
  'Sep',
  'Okt',
  'Nov',
  'Des',
];

String _two(int v) => v.toString().padLeft(2, '0');

/// DateTime (sudah UTC, isUtc=true) -> string ISO yang dimakan backend.
String isoUtc(DateTime u) =>
    '${u.year.toString().padLeft(4, '0')}-${_two(u.month)}-${_two(u.day)}'
    'T${_two(u.hour)}:${_two(u.minute)}:${_two(u.second)}Z';

String fmtDayMonth(String? isoUtcStr, AppTimeZone tz) {
  if (isoUtcStr == null) return 'awal';
  final u = DateTime.parse(isoUtcStr);
  final l = tz.utcToLocal(u);
  return '${l.day} ${_bulan[l.month - 1]} ${l.year}';
}

/// Hitung rentang UTC untuk preset yang dipilih.
///
/// [nowUtc] diinjeksi agar test deterministik. Untuk preset custom,
/// [customStart]/[customEnd] adalah tanggal dinding-waktu di zona [tz];
/// return null jika belum lengkap/invalid (pemanggil menampilkan error).
Mt5DateRange? rangeFor(
  DatePreset preset,
  AppTimeZone tz,
  DateTime nowUtc, {
  DateTime? customStart,
  DateTime? customEnd,
}) {
  assert(nowUtc.isUtc, 'nowUtc harus UTC');
  switch (preset) {
    case DatePreset.d7:
    case DatePreset.d30:
    case DatePreset.d90:
      final days = switch (preset) {
        DatePreset.d7 => 7,
        DatePreset.d30 => 30,
        _ => 90,
      };
      final to = nowUtc;
      final from = to.subtract(Duration(days: days));
      return Mt5DateRange(isoUtc(from), isoUtc(to), '$days hari terakhir');

    case DatePreset.all:
      return const Mt5DateRange(null, null, 'Semua riwayat');

    case DatePreset.thisMonth:
    case DatePreset.lastMonth:
      final nowLocal = tz.utcToLocal(nowUtc);
      final firstThis = DateTime(nowLocal.year, nowLocal.month, 1);
      final firstNext = (nowLocal.month == 12)
          ? DateTime(nowLocal.year + 1, 1, 1)
          : DateTime(nowLocal.year, nowLocal.month + 1, 1);
      final firstPrev = (nowLocal.month == 1)
          ? DateTime(nowLocal.year - 1, 12, 1)
          : DateTime(nowLocal.year, nowLocal.month - 1, 1);
      final fromLocal = preset == DatePreset.thisMonth ? firstThis : firstPrev;
      final toLocal = preset == DatePreset.thisMonth ? firstNext : firstThis;
      final name =
          '${_bulan[fromLocal.month - 1]} ${fromLocal.year} (${tz.id == 'UTC' ? 'UTC' : tz.label.split('·').first.trim()})';
      return Mt5DateRange(
        isoUtc(tz.localToUtc(fromLocal).toUtc()),
        isoUtc(tz.localToUtc(toLocal).toUtc()),
        name,
      );

    case DatePreset.custom:
      if (customStart == null || customEnd == null) return null;
      final startLocal = DateTime(
        customStart.year,
        customStart.month,
        customStart.day,
      );
      final endLocal = DateTime(
        customEnd.year,
        customEnd.month,
        customEnd.day,
      ).add(const Duration(days: 1)); // eksklusif: seluruh hari akhir
      if (startLocal.isAfter(endLocal)) return null;
      final fromU = tz.localToUtc(startLocal).toUtc();
      final toU = tz.localToUtc(endLocal).toUtc();
      if (fromU.isAfter(toU)) return null; // invalid (mis. DST-like edge)
      return Mt5DateRange(
        isoUtc(fromU),
        isoUtc(toU),
        '${customStart.day} ${_bulan[customStart.month - 1]} – '
        '${customEnd.day} ${_bulan[customEnd.month - 1]} '
        '${customEnd.year}',
      );
  }
}
