export interface GitCommit {
  hash: string;
  parents: string[];
  refs: string[];
  author: string;
  authored_at: string;
  subject: string;
}

export interface GitGraphRow {
  commit: GitCommit;
  lane: number;
  lanesBefore: string[];
  lanesAfter: string[];
  isNewTip: boolean;
}

export function buildGitGraphRows(commits: GitCommit[]): GitGraphRow[] {
  let lanes: string[] = [];

  return commits.map((commit) => {
    const lanesBefore = [...lanes];
    let lane = lanesBefore.indexOf(commit.hash);
    const isNewTip = lane === -1;
    if (isNewTip) {
      lane = 0;
      lanesBefore.unshift(commit.hash);
    }

    const lanesAfter = lanesBefore.filter((_, index) => index !== lane);
    commit.parents.forEach((parent, parentIndex) => {
      if (lanesAfter.includes(parent)) return;
      lanesAfter.splice(Math.min(lane + parentIndex, lanesAfter.length), 0, parent);
    });

    lanes = lanesAfter;
    return { commit, lane, lanesBefore, lanesAfter, isNewTip };
  });
}

export function graphLaneCount(rows: GitGraphRow[]): number {
  return Math.max(
    1,
    ...rows.map((row) => Math.max(row.lanesBefore.length, row.lanesAfter.length)),
  );
}
