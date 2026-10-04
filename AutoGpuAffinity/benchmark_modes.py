"""Recorded, repeatable schedules with real physical SMT pairs."""
import random


def schedule(cpus, topology, mode, seed=None):
    cpus = sorted(set(cpus))
    def job(phase, members):
        label = 'CPU-' + '+'.join(map(str, members))
        return {'id': f'{phase}:{label}', 'phase': phase, 'cpus': members,
                'label': label, 'directory': {'normal': 'CSVs', 'paired': 'pairs', 'shuffled': 'shuffled'}[phase],
                'artifact': label if phase == 'normal' else f'{phase}-{label}'}
    jobs = [job('normal', [cpu]) for cpu in cpus]
    skipped = []
    if mode == 'enhanced':
        if any(core['group'] != 0 for core in topology['cores']):
            raise ValueError('增强模式暂不支持跨处理器组。')
        for core in topology['cores']:
            members = sorted(core['cpus'])
            if len(members) == 2 and core['smt'] and members[0] % 2 == 0 and members[1] == members[0] + 1:
                if set(members) <= set(cpus):
                    jobs.append(job('paired', members))
                else:
                    skipped.append({'cpus': members, 'reason': '超线程组未完整包含在测试范围中'})
            elif core['smt']:
                skipped.append({'cpus': members, 'reason': '实际拓扑不符合偶数起始双线程规则'})
        seed = random.SystemRandom().getrandbits(64) if seed is None else seed
        shuffled = cpus.copy()
        random.Random(seed).shuffle(shuffled)
        if len(shuffled) > 1 and shuffled == cpus:
            shuffled = shuffled[1:] + shuffled[:1]
        jobs.extend(job('shuffled', [cpu]) for cpu in shuffled)
    return {'mode': mode, 'seed': seed, 'jobs': jobs, 'skipped_pairs': skipped}
