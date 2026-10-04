import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
from gpu import Gpu, select_gpu, match_vulkan
from config import Config


class SelectionTests(unittest.TestCase):
    def test_mining_cards_with_display_gpu(self):
        output = Gpu('AMD Radeon HD 7450', r'PCI\VEN_1002&DEV_677B\A')
        for name in ['NVIDIA P106-100', 'NVIDIA CMP 30HX', 'NVIDIA CMP 40HX', 'NVIDIA 40 HX']:
            mining = Gpu(name, r'PCI\VEN_10DE&DEV_1C07\B')
            self.assertEqual(select_gpu([output, mining]), mining)

    def test_ambiguous_selection_requires_user_choice(self):
        cards = [Gpu('NVIDIA CMP 30HX', 'a'), Gpu('NVIDIA CMP 40HX', 'b')]
        with self.assertRaises(ValueError):
            select_gpu(cards)
        self.assertEqual(select_gpu(cards, '40HX'), cards[1])
        with self.assertRaises(ValueError):
            select_gpu(cards, 'CMP')

    def test_ordinary_multi_gpu_is_not_guessed(self):
        with self.assertRaises(ValueError):
            select_gpu([Gpu('Intel HD', 'a'), Gpu('NVIDIA GTX 1060', 'b')])
        self.assertEqual(select_gpu([Gpu('GTX 1060', 'a')]).hwid, 'a')

    def test_vulkan_order_is_independent_of_wmi(self):
        card = Gpu('P106', r'PCI\VEN_10DE&DEV_1C07\B')
        self.assertEqual(match_vulkan(card, [(0, 0x1002, 0x677B, 'AMD'), (1, 0x10DE, 0x1C07, 'P106')]), 1)
        with self.assertRaises(ValueError):
            match_vulkan(card, [(0, 0x1002, 0x677B, 'AMD')])
        with self.assertRaises(ValueError):
            match_vulkan(card, [(0, 0x10DE, 0x1C07, 'A'), (1, 0x10DE, 0x1C07, 'B')])

    def test_shipped_configs_enable_new_features(self):
        root = Path(__file__).resolve().parents[1]
        for name in ['config.ini', 'AutoGpuAffinity/config.ini']:
            cfg = Config(str(root / name))
            self.assertEqual(cfg.settings.gpu, 'auto')
            self.assertTrue(cfg.settings.auto_resolution)
            self.assertTrue(cfg.settings.force_fullscreen)
            self.assertEqual(cfg.settings.custom_cpus, [])

if __name__ == '__main__':
    unittest.main()
