/**
 * Fixture data for #/preview and vitest only — never imported by the store or
 * live services. Deterministic history (seeded LCG) so tests are stable.
 */

import type { GpuHistorySeries } from "../store/consoleStore";
import type {
  FleetEntry,
  FleetSummary,
  GpuInfo,
  GpuProcessInfo,
  ProcessInfo,
  ServerRecord,
  ServerSnapshot,
} from "../types/models";

const NOW = Date.now();
const NOW_ISO = new Date(NOW).toISOString();

const GPU_UUID_0 = "GPU-3f9c2a10-9d44-2f6e-b1a5-0c4f7e8d1234";
const GPU_UUID_1 = "GPU-7b2e5c88-1a30-4d9f-8e2b-9a6c1d3f5678";
const A100_UUIDS = Array.from(
  { length: 8 },
  (_unused, index) => `GPU-a100-${String(index).padStart(4, "0")}-0000-0000-0000`,
);

// -- server 1: 2-GPU RTX 4090 box, one busy GPU ------------------------------

const lab4090Gpus: GpuInfo[] = [
  {
    index: 0,
    uuid: GPU_UUID_0,
    name: "NVIDIA GeForce RTX 4090",
    utilization_percent: 72.4,
    vram_used_b: 19541268941,
    vram_total_b: 25769803776,
    vram_percent: 75.8,
    temperature_c: 64,
    power_watts: 210,
    power_limit_watts: 350,
    fan_percent: 55,
    availability: "active",
    process_count: 1,
  },
  {
    index: 1,
    uuid: GPU_UUID_1,
    name: "NVIDIA GeForce RTX 4090",
    utilization_percent: 3.5,
    vram_used_b: 629145600,
    vram_total_b: 25769803776,
    vram_percent: 2.7,
    temperature_c: 38,
    power_watts: 21,
    power_limit_watts: 350,
    fan_percent: 30,
    availability: "free",
    process_count: 0,
  },
];

const lab4090GpuProcesses: GpuProcessInfo[] = [
  {
    pid: 40211,
    gpu_uuid: GPU_UUID_0,
    gpu_index: 0,
    used_memory_b: 19116342497,
    process_name: "python",
    user: "demo",
    command: "python train_cifar.py --batch-size 256 --epochs 120",
  },
];

const lab4090Processes: ProcessInfo[] = [
  {
    pid: 40211,
    user: "demo",
    name: "python",
    cpu_percent: 712.4,
    mem_percent: 21.4,
    rss_b: 5583459725,
    state: "R",
    command: "python train_cifar.py --batch-size 256 --epochs 120",
    gpu_indexes: [0],
    gpu_vram_b: 19116342497,
  },
  {
    pid: 1180,
    user: "dana",
    name: "code",
    cpu_percent: 12.1,
    mem_percent: 8.2,
    rss_b: 1_980_000_000,
    state: "S",
    command: "/usr/share/code/code --type=renderer",
    gpu_indexes: [],
    gpu_vram_b: null,
  },
  {
    pid: 902,
    user: "root",
    name: "systemd-journal",
    cpu_percent: 0.4,
    mem_percent: 0.3,
    rss_b: 74_000_000,
    state: "S",
    command: "/lib/systemd/systemd-journald",
    gpu_indexes: [],
    gpu_vram_b: null,
  },
  {
    pid: 2210,
    user: "demo",
    name: "zsh",
    cpu_percent: 0.1,
    mem_percent: 0.1,
    rss_b: 22_000_000,
    state: "S",
    command: "zsh",
    gpu_indexes: [],
    gpu_vram_b: null,
  },
  {
    pid: 3311,
    user: "dana",
    name: "htop",
    cpu_percent: 0.6,
    mem_percent: 0.2,
    rss_b: 18874368,
    state: "S",
    command: "htop",
    gpu_indexes: [],
    gpu_vram_b: null,
  },
];

// -- server 2: 8-GPU saturated A100 box --------------------------------------

const dgxGpus: GpuInfo[] = A100_UUIDS.map((uuid, index) => ({
  index,
  uuid,
  name: "NVIDIA A100-SXM4-80GB",
  utilization_percent: 93 + (index % 5) * 1.4,
  vram_used_b: 83_852_621_000 + index * 220_200_960,
  vram_total_b: 85_899_345_920,
  vram_percent: 98.1 - (index % 3) * 0.4,
  temperature_c: 71 + (index % 4) * 2,
  power_watts: 288 + index * 3.2,
  power_limit_watts: 400,
  fan_percent: 78,
  availability: "saturated",
  process_count: 1,
}));

const dgxGpuProcesses: GpuProcessInfo[] = A100_UUIDS.map((uuid, index) => ({
  pid: 50_000 + index * 37,
  gpu_uuid: uuid,
  gpu_index: index,
  used_memory_b: (52 + (index % 4) * 6) * 1024 ** 3 + index * 140_000_000,
  process_name: "python",
  user: index % 2 === 0 ? "demo" : "maria",
  command: "python -m torch.distributed.run --nproc_per_node=8 bench.py",
}));

const dgxProcesses: ProcessInfo[] = dgxGpuProcesses.slice(0, 5).map((proc, index) => ({
  pid: proc.pid,
  user: proc.user,
  name: "python",
  cpu_percent: 640 - index * 30,
  mem_percent: 11.2 + index * 0.8,
  rss_b: 12_800_000_000 + index * 400_000_000,
  state: "R",
  command: "python -m torch.distributed.run --nproc_per_node=8 bench.py",
  gpu_indexes: [index],
  gpu_vram_b: proc.used_memory_b,
}));

// -- records -------------------------------------------------------------------

export const fixtureServers: ServerRecord[] = [
  {
    server_id: "srv-lab-4090",
    display_name: "lab-4090",
    ssh_host: "lab-4090",
    username: "demo",
    port: 1111,
    tags: ["lab"],
    enabled: true,
  },
  {
    server_id: "srv-dgx-a100",
    display_name: "dgx-a100",
    ssh_host: "dgx-a100",
    username: "ops",
    port: 22,
    tags: ["cluster"],
    enabled: true,
  },
  {
    server_id: "srv-gpu-node-3",
    display_name: "gpu-node-3",
    ssh_host: "gpu-node-3",
    username: "demo",
    port: null,
    tags: [],
    enabled: true,
  },
];

const SSH_CLOSED = "ssh_closed";

export const fixtureSnapshots: Record<string, ServerSnapshot> = {
  "srv-lab-4090": {
    server_id: "srv-lab-4090",
    status: "online",
    generated_at: NOW_ISO,
    cpu: { percent: 23.4, load_1: 2.14, load_5: 1.98, load_15: 1.76, cores_logical: 32 },
    memory: {
      total_b: 68_700_000_000,
      used_b: 28_100_000_000,
      available_b: 40_600_000_000,
      percent: 40.9,
    },
    gpus: lab4090Gpus,
    gpu_processes: lab4090GpuProcesses,
    processes: lab4090Processes,
    storage: [
      {
        device: "/dev/nvme0n1p2",
        fstype: "ext4",
        mount: "/",
        total_b: 984_000_000_000,
        used_b: 590_000_000_000,
        free_b: 344_000_000_000,
        percent: 60,
      },
      {
        device: "/dev/sda1",
        fstype: "ext4",
        mount: "/data",
        total_b: 8796093022208,
        used_b: 7212796278000,
        free_b: 1099511627776,
        percent: 82,
      },
      {
        device: "/dev/nvme0n1p1",
        fstype: "ext4",
        mount: "/home",
        total_b: 512_000_000_000,
        used_b: 159_000_000_000,
        free_b: 327_000_000_000,
        percent: 31,
      },
    ],
    network: [
      {
        name: "eth0",
        ip: "10.40.0.11",
        rx_bps: 1_240_000,
        tx_bps: 248832,
        rx_total_b: 10745058181000,
        tx_total_b: 3221225473000,
      },
      {
        name: "lo",
        ip: "127.0.0.1",
        rx_bps: 12_000,
        tx_bps: 12_000,
        rx_total_b: 88_000_000_000,
        tx_total_b: 88_000_000_000,
      },
    ],
    system: {
      hostname: "lab-4090",
      os_pretty: "Ubuntu 22.04.4 LTS",
      kernel: "5.15.0-107-generic",
      uptime_s: 1_036_800,
      cpu_model: "AMD Ryzen 9 7950X 16-Core Processor",
      cores_physical: 16,
      cores_logical: 32,
      driver_version: "550.54.15",
    },
    errors: {},
    stale: false,
  },

  "srv-dgx-a100": {
    server_id: "srv-dgx-a100",
    status: "online",
    generated_at: NOW_ISO,
    cpu: { percent: 87.6, load_1: 61.2, load_5: 58.9, load_15: 55.1, cores_logical: 128 },
    memory: {
      total_b: 1_099_511_627_776,
      used_b: 838_000_000_000,
      available_b: 261_000_000_000,
      percent: 76.2,
    },
    gpus: dgxGpus,
    gpu_processes: dgxGpuProcesses,
    processes: dgxProcesses,
    storage: [
      {
        device: "/dev/nvme0n1p2",
        fstype: "ext4",
        mount: "/",
        total_b: 1_900_000_000_000,
        used_b: 836_000_000_000,
        free_b: 968_000_000_000,
        percent: 44,
      },
      {
        device: "/dev/raid0",
        fstype: "xfs",
        mount: "/scratch",
        total_b: 28796093022208,
        used_b: 25_480_000_000_000,
        free_b: 1_960_000_000_000,
        percent: 91,
      },
    ],
    network: [
      {
        name: "ib0",
        ip: "10.41.0.2",
        rx_bps: 880_000_000,
        tx_bps: 912_000_000,
        rx_total_b: 812_000_000_000_000,
        tx_total_b: 801_000_000_000_000,
      },
    ],
    system: {
      hostname: "dgx-a100",
      os_pretty: "Ubuntu 22.04.4 LTS",
      kernel: "5.15.0-105-generic",
      uptime_s: 5_443_200,
      cpu_model: "AMD EPYC 7742 64-Core Processor",
      cores_physical: 64,
      cores_logical: 128,
      driver_version: "535.104.05",
    },
    errors: {},
    stale: false,
  },

  "srv-gpu-node-3": {
    server_id: "srv-gpu-node-3",
    status: "offline",
    generated_at: NOW_ISO,
    cpu: null,
    memory: null,
    gpus: [],
    gpu_processes: [],
    processes: [],
    storage: [],
    network: [],
    system: null,
    errors: {
      gpu: SSH_CLOSED,
      cpu: SSH_CLOSED,
      memory: SSH_CLOSED,
      gpu_processes: SSH_CLOSED,
      processes: SSH_CLOSED,
      storage: SSH_CLOSED,
      network: SSH_CLOSED,
      system: SSH_CLOSED,
    },
    stale: false,
  },
};

const fixtureFleetEntries: FleetEntry[] = [
  {
    server_id: "srv-lab-4090",
    display_name: "lab-4090",
    ssh_endpoint: "demo@lab-4090:1111",
    status: "online",
    enabled: true,
    os_pretty: "Ubuntu 22.04.4 LTS",
    gpu_model: "RTX 4090",
    gpu_count: 2,
    gpu_busy: 1,
    gpu_free: 1,
    cpu_percent: 23.4,
    memory_percent: 40.9,
    vram_used_b: 20_185_003_460,
    vram_total_b: 51539607552,
    disk_warning: true,
    updated_at: NOW_ISO,
  },
  {
    server_id: "srv-dgx-a100",
    display_name: "dgx-a100",
    ssh_endpoint: "ops@dgx-a100:22",
    status: "online",
    enabled: true,
    os_pretty: "Ubuntu 22.04.4 LTS",
    gpu_model: "A100-SXM4-80GB",
    gpu_count: 8,
    gpu_busy: 8,
    gpu_free: 0,
    cpu_percent: 87.6,
    memory_percent: 76.2,
    vram_used_b: 627_600_000_000,
    vram_total_b: 636_800_000_000,
    disk_warning: true,
    updated_at: NOW_ISO,
  },
  {
    server_id: "srv-gpu-node-3",
    display_name: "gpu-node-3",
    ssh_endpoint: "demo@gpu-node-3:22",
    status: "offline",
    enabled: true,
    os_pretty: null,
    gpu_model: null,
    gpu_count: 0,
    gpu_busy: 0,
    gpu_free: 0,
    cpu_percent: null,
    memory_percent: null,
    vram_used_b: null,
    vram_total_b: null,
    disk_warning: false,
    updated_at: NOW_ISO,
  },
];

export const fixtureFleetSummary: FleetSummary = {
  generated_at: NOW_ISO,
  servers: fixtureFleetEntries,
};

// -- deterministic history -----------------------------------------------------

function hashSeed(key: string): number {
  let hash = 2_166_136_261;
  for (let i = 0; i < key.length; i += 1) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 16_777_619);
  }
  return hash >>> 0;
}

function lcg(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

interface HistoryBases {
  util: number;
  vram: number;
  temp: number;
  power: number;
}

const HISTORY_BASES: Record<string, HistoryBases> = {
  "srv-lab-4090:0": { util: 70, vram: 75.8, temp: 62, power: 205 },
  "srv-lab-4090:1": { util: 3, vram: 2.7, temp: 37, power: 20 },
  ...Object.fromEntries(
    A100_UUIDS.map((_uuid, index) => [
      `srv-dgx-a100:${index}`,
      { util: 94 + (index % 3), vram: 98 - (index % 2), temp: 73, power: 295 + index },
    ]),
  ),
};

function series(base: number, amp: number, noise: number, phase: number, max: number, rnd: () => number): number[] {
  return Array.from({ length: 120 }, (_unused, i) =>
    Math.round(clamp(base + amp * Math.sin(i / 11 + phase) + (rnd() - 0.5) * noise, 0, max) * 10) / 10,
  );
}

/** 120 deterministic samples per metric for a fixture GPU (empty when unknown). */
export function fixtureGpuHistory(serverId: string, gpuIndex: number): GpuHistorySeries {
  const key = `${serverId}:${gpuIndex}`;
  const bases = HISTORY_BASES[key];
  if (bases === undefined) {
    return { utilization: [], vram: [], temperature: [], power: [] };
  }
  const rnd = lcg(hashSeed(key));
  const phase = (hashSeed(key) % 628) / 100;
  return {
    utilization: series(bases.util, 12, 10, phase, 100, rnd),
    vram: series(bases.vram, 1.5, 1, phase / 3, 100, rnd),
    temperature: series(bases.temp, 4, 2, phase / 2, 100, rnd),
    power: series(bases.power, 20, 16, phase, 600, rnd),
  };
}
