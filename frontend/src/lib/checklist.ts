import type { InspectionStatus } from '../types/api';

export const INSPECTION_LABELS: Record<InspectionStatus, string> = {
  NOT_STARTED: 'Not started',
  IN_PROGRESS: 'In progress',
  PASSED: 'Passed',
};
