// Transcendence — ui/navigation.dart
// KOMPONEN NAVIGASI: header sapaan (dashboard), header halaman (sub-tab),
// tombol tema, dan bottom navigation pill. Murni presentasi: tab aktif dan
// aksi diberikan pemanggil lewat parameter/callback.
import 'package:flutter/material.dart';

import '../ui_kit.dart';

/// Item bottom navigation.
class PillNavItem {
  final IconData icon;
  final IconData activeIcon;
  final String label;
  const PillNavItem(this.icon, this.activeIcon, this.label);
}

/// Bottom navigation: bar pill gelap mengambang, ikon saja, tab aktif =
/// pill biru. Label tersedia lewat Tooltip (tekan lama) + Semantics.
class PillNavBar extends StatelessWidget {
  final List<PillNavItem> items;
  final int index;
  final ValueChanged<int> onTap;
  const PillNavBar({
    super.key,
    required this.items,
    required this.index,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    // Saat keyboard terbuka, bar disembunyikan agar tidak ikut naik dan
    // menutupi kolom input yang sedang diketik.
    // Dibaca langsung dari View: Scaffold menolkan viewInsets untuk body.
    if (View.of(context).viewInsets.bottom > 0) {
      return const SizedBox.shrink();
    }
    final safe = MediaQuery.of(context).padding.bottom;
    return Container(
      color: p.canvas,
      padding: EdgeInsets.fromLTRB(40, 4, 40, 8 + safe),
      child: Container(
        height: 54,
        padding: const EdgeInsets.symmetric(horizontal: 6),
        decoration: BoxDecoration(
          color: p.navBg,
          borderRadius: BorderRadius.circular(27),
          border: Border.all(width: 1, color: p.navBorder),
          boxShadow: [
            BoxShadow(
              color: p.isDark ? p.shadow : const Color(0x330D1321),
              blurRadius: 22,
              spreadRadius: -6,
              offset: const Offset(0, 8),
            ),
          ],
        ),
        child: Row(
          children: [
            for (int i = 0; i < items.length; i++) Expanded(child: _item(p, i)),
          ],
        ),
      ),
    );
  }

  Widget _item(Palette p, int i) {
    final it = items[i];
    final active = index == i;
    return Tooltip(
      message: it.label,
      child: Semantics(
        button: true,
        selected: active,
        child: InkResponse(
          onTap: () => onTap(i),
          radius: 30,
          child: Center(
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 240),
              curve: Curves.easeOutCubic,
              width: active ? 50 : 38,
              height: active ? 40 : 38,
              decoration: BoxDecoration(
                color: active ? p.goldSolid : Colors.transparent,
                borderRadius: BorderRadius.circular(20),
                border: active
                    ? Border.all(
                        width: 2,
                        color: p.goldSolid.withAlpha(70),
                        strokeAlign: BorderSide.strokeAlignOutside,
                      )
                    : null,
                boxShadow: active
                    ? [
                        BoxShadow(
                          color: p.goldSolid.withAlpha(110),
                          blurRadius: 16,
                          spreadRadius: -4,
                        ),
                      ]
                    : null,
              ),
              alignment: Alignment.center,
              child: Icon(
                active ? it.activeIcon : it.icon,
                size: active ? 20 : 19,
                color: active ? p.onGold : p.navIdle,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Tombol bulat netral (back, aksi header).
class CircleIconButton extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;
  final double size;
  const CircleIconButton({
    super.key,
    required this.icon,
    required this.tooltip,
    required this.onTap,
    this.size = 42,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Tooltip(
      message: tooltip,
      child: Material(
        color: p.card,
        shape: CircleBorder(
          side: BorderSide(color: p.isDark ? p.line : p.line),
        ),
        child: InkWell(
          customBorder: const CircleBorder(),
          onTap: onTap,
          child: SizedBox(
            width: size,
            height: size,
            child: Icon(icon, size: size * 0.42, color: p.ink),
          ),
        ),
      ),
    );
  }
}

/// Tombol pill tema (ikon mode + chevron) — membuka pengaturan tampilan.
class ThemePillButton extends StatelessWidget {
  final VoidCallback onTap;
  const ThemePillButton({super.key, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Tooltip(
      message: 'Tema tampilan',
      child: Material(
        color: p.card,
        shape: StadiumBorder(side: BorderSide(color: p.line)),
        child: InkWell(
          customBorder: const StadiumBorder(),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 9),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  p.isDark
                      ? Icons.dark_mode_outlined
                      : Icons.light_mode_outlined,
                  size: 19,
                  color: p.ink,
                ),
                const SizedBox(width: 3),
                Icon(Icons.expand_more, size: 17, color: p.muted),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Header dashboard gaya referensi: sapaan + nama app + subjudul di kiri,
/// avatar + tombol tema di kanan.
class GreetingHeader extends StatelessWidget {
  final String greeting;
  final String title;
  final String subtitle;
  final VoidCallback onTheme;
  const GreetingHeader({
    super.key,
    required this.greeting,
    required this.title,
    required this.subtitle,
    required this.onTheme,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final narrow = MediaQuery.sizeOf(context).width < 380;
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 14, 16, 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  greeting,
                  style: txt(
                    context,
                    narrow ? 20 : 23,
                    w: FontWeight.w500,
                    color: p.ink,
                    ls: -0.4,
                    height: 1.15,
                  ),
                ),
                Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: txt(
                    context,
                    narrow ? 22 : 25,
                    w: FontWeight.w700,
                    ls: -0.6,
                    height: 1.15,
                  ),
                ),
                const SizedBox(height: 6),
                Row(
                  children: [
                    PulseDot(p.mint, size: 6),
                    const SizedBox(width: 6),
                    Flexible(
                      child: Text(
                        subtitle,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: txt(context, 12.5, color: p.muted),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(width: 10),
          Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              border: Border.all(width: 2, color: p.goldSolid),
            ),
            padding: const EdgeInsets.all(2),
            child: ClipOval(
              child: Image.asset(
                'assets/icon/icon.png',
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) =>
                    Icon(Icons.person, color: p.gold, size: 20),
              ),
            ),
          ),
          const SizedBox(width: 8),
          ThemePillButton(onTap: onTheme),
        ],
      ),
    );
  }
}

/// Header halaman (sub-tab) gaya referensi: tombol kembali bulat, judul di
/// tengah, aksi di kanan.
class PageHeader extends StatelessWidget {
  final String title;
  final VoidCallback onBack;
  final VoidCallback onTheme;
  const PageHeader({
    super.key,
    required this.title,
    required this.onBack,
    required this.onTheme,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
      child: Row(
        children: [
          CircleIconButton(
            icon: Icons.arrow_back_ios_new,
            tooltip: 'Kembali ke Dashboard',
            onTap: onBack,
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              title,
              textAlign: TextAlign.center,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: txt(context, 17, w: FontWeight.w600, ls: -0.2),
            ),
          ),
          const SizedBox(width: 10),
          ThemePillButton(onTap: onTheme),
        ],
      ),
    );
  }
}
