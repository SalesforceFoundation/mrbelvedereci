import {
  FilterDefinition,
  TestMethodPerfUI,
} from 'api/testmethod_perf_UI_JSON_schema';
import { AppState } from 'store';
import {
  LoadingStatus,
  PerfData_UI_State,
  PerfDataState,
} from 'store/perfdata/reducer';

export const selectPerfState = (appState: AppState): PerfDataState =>
  appState.perfData;

export const selectPerfUIState = (appState: AppState): PerfData_UI_State =>
  appState.perfDataUI;

export const selectPerfUIStatus = (appState: AppState): LoadingStatus =>
  appState.perfDataUI ? appState.perfDataUI.status : 'LOADING';

export const selectPerfDataAPIUrl = (appState: AppState): string =>
  (appState.perfData && appState.perfData.url) || '';

export const selectTestMethodPerfUI = (
  appState: AppState,
): TestMethodPerfUI | null =>
  appState.perfDataUI.status === 'AVAILABLE'
    ? appState.perfDataUI.uidata.testmethod_perf
    : null;

export const selectTestMethodResultsUI = (
  appState: AppState,
): TestMethodPerfUI | null =>
  appState.perfDataUI.status === 'AVAILABLE'
    ? appState.perfDataUI.uidata.testmethod_results
    : null;

export const selectBuildflowFiltersUI = (
  appState: AppState,
): FilterDefinition[] | null =>
  appState.perfDataUI.status === 'AVAILABLE'
    ? appState.perfDataUI.uidata.buildflow_filters
    : null;

export const selectDebugStatus = (appState: AppState): boolean =>
  appState.perfDataUI.status === 'AVAILABLE'
    ? appState.perfDataUI.uidata.debug
    : false;
