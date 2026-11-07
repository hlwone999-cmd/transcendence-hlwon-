// Transcendence — mt5_servers.dart
// Daftar pilihan server broker untuk dropdown Jurnal MT5 (keputusan D4).
//
// KEJUJURAN DAFTAR (D4): daftar Exness di bawah HANYA pilihan yang umum —
// TIDAK dianggap lengkap dan TIDAK sama untuk semua akun. UI wajib
// menyediakan opsi [kServerLainnya] dengan input manual, sehingga akun
// dengan server apa pun tetap bisa dihubungkan.
//
// File ini murni data — tidak ada logika jaringan dan tidak menyentuh
// kalkulator lot (lib/engine.dart, lib/montecarlo.dart, main.dart —
// protected).
library;

/// Opsi "Lainnya" pada dropdown — memunculkan TextField server manual.
const String kServerLainnya = '__LAINNYA__';

class Mt5ServerOption {
  final String label; // yang tampil di dropdown
  final String value; // nilai yang dikirim ke backend (server MT5)

  const Mt5ServerOption(this.label, this.value);

  @override
  bool operator ==(Object other) =>
      other is Mt5ServerOption && other.value == value;

  @override
  int get hashCode => value.hashCode;
}

/// Pilihan server Exness yang umum (NON-EKSAUSTIF — lihat catatan di atas).
/// Server nyata akun bisa berbeda (mis. Exness-MT5Real5, Exness-MT5Real12);
/// customer memakai Lainnya bila servernya tidak ada di daftar.
const List<Mt5ServerOption> kExnessServers = [
  Mt5ServerOption('Exness-MT5Real', 'Exness-MT5Real'),
  Mt5ServerOption('Exness-MT5Real8', 'Exness-MT5Real8'),
  Mt5ServerOption('Exness-MT5Trial', 'Exness-MT5Trial'),
  Mt5ServerOption('Exness-MT5Trial7', 'Exness-MT5Trial7'),
];

/// Semua opsi dropdown: server Exness + opsi Lainnya di paling bawah.
const List<Mt5ServerOption> kServerOptions = [
  ...kExnessServers,
  Mt5ServerOption('Lainnya (isi manual)', kServerLainnya),
];

/// Cari label dropdown dari nilai server tersimpan (untuk prafill);
/// nilai di luar daftar dipetakan ke Lainnya.
String labelForServerValue(String value) {
  for (final o in kServerOptions) {
    if (o.value == value && o.value != kServerLainnya) return o.label;
  }
  return 'Lainnya (isi manual)';
}

/// Validasi ringan server manual (input Lainnya): wajib diisi, 1..128
/// karakter — sama dengan batas backend. Return pesan galat atau null.
String? validateManualServer(String raw) {
  final s = raw.trim();
  if (s.isEmpty) return 'Nama server wajib diisi';
  if (s.length > 128) return 'Nama server terlalu panjang (maks 128)';
  return null;
}
