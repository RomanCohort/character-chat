"""旋钮白名单 —— 由 gen_torus_schema.py 从 GET /api/schema 生成，勿手改。

KNOBS: name -> (kind, min, max)。kind ∈ int/float/bool/text。
DEFAULTS: name -> 默认值（页面表单初值）。
GROUPS: 组名 -> [旋钮名]，保持官方分组顺序。
"""

KNOBS = {
    "max_seg_len":         ('int', 50, 1000),
    "overlap":             ('int', 0, 100),
    "n_relax_rounds":      ('int', 0, 60),
    "n_parallel":          ('int', 0, 64),
    "n_rest2_replicas":    ('int', 2, 64),
    "rest2_nsteps":        ('int', 5000, 2000000),
    "md_step_scale":       ('float', 0.01, 1.0),
    "nrep":                ('int', 1, 64),
    "platform":            ('text', None, None),
    "use_5bead":           ('bool', None, None),
    "use_metad":           ('bool', None, None),
    "metad_n_steps":       ('int', 0, 2000000),
    "use_rl_relax":        ('bool', None, None),
    "use_rl_mcts":         ('bool', None, None),
    "rl_n_simulations":    ('int', 0, 500),
    "use_rhofold":         ('bool', None, None),
    "n_candidates":        ('int', 1, 10),
    "use_msa":             ('bool', None, None),
    "use_pyrosetta":       ('bool', None, None),
    "use_ppr":             ('bool', None, None),
    "ppr_max_rounds":      ('int', 0, 20),
    "resume":              ('bool', None, None),
    "use_multi_task_heads": ('bool', None, None),
    "use_structrfm":       ('bool', None, None),
    "multitask_head_weights": ('text', None, None),
    "ss_head_weight":      ('float', 0, 10),
    "pair_head_weight":    ('float', 0, 10),
    "bsj_head_weight":     ('float', 0, 10),
    "clash_head_weight":   ('float', 0, 10),
    "use_rcm_reweight":    ('bool', None, None),
}

DEFAULTS = {
    "max_seg_len":         200,
    "overlap":             20,
    "n_relax_rounds":      6,
    "n_parallel":          0,
    "n_rest2_replicas":    16,
    "rest2_nsteps":        300000,
    "md_step_scale":       0.1,
    "nrep":                1,
    "platform":            'auto',
    "use_5bead":           True,
    "use_metad":           True,
    "metad_n_steps":       200000,
    "use_rl_relax":        True,
    "use_rl_mcts":         True,
    "rl_n_simulations":    50,
    "use_rhofold":         False,
    "n_candidates":        1,
    "use_msa":             True,
    "use_pyrosetta":       True,
    "use_ppr":             True,
    "ppr_max_rounds":      5,
    "resume":              True,
    "use_multi_task_heads": False,
    "use_structrfm":       False,
    "multitask_head_weights": None,
    "ss_head_weight":      1.0,
    "pair_head_weight":    1.0,
    "bsj_head_weight":     0.5,
    "clash_head_weight":   0.3,
    "use_rcm_reweight":    False,
}

GROUPS = {
    'Segmentation': ['max_seg_len', 'overlap', 'n_relax_rounds', 'n_parallel'],
    'Sampling': ['n_rest2_replicas', 'rest2_nsteps', 'md_step_scale', 'nrep', 'platform'],
    'Enhanced sampling': ['use_5bead', 'use_metad', 'metad_n_steps'],
    'Guidance & repair': ['use_rl_relax', 'use_rl_mcts', 'rl_n_simulations'],
    'Prediction source': ['use_rhofold', 'n_candidates', 'use_msa'],
    'Optional stages': ['use_pyrosetta', 'use_ppr', 'ppr_max_rounds', 'resume'],
    'structRFM heads': ['use_multi_task_heads', 'use_structrfm', 'multitask_head_weights', 'ss_head_weight', 'pair_head_weight', 'bsj_head_weight', 'clash_head_weight'],
    'Method variants': ['use_rcm_reweight'],
}

KNOB_COUNT = 30
PIPELINE = 'torusfold.scheme2.isrnaclong.isrnaclong_pipeline'
OPEN_BY_DEFAULT = ['Segmentation', 'Sampling']
