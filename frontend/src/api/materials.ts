import { apiFetch } from './client';

export interface MaterialAvailability {
  readonly material_id: string;
  readonly name: string;
  readonly unit: string;
  readonly quantity_available: string;
  readonly reserved_quantity: string;
  readonly input_snapshot_version: number;
}

export function getMaterial(materialId: string): Promise<MaterialAvailability> {
  return apiFetch<MaterialAvailability>(`/materials/${encodeURIComponent(materialId)}`);
}

export function updateMaterialAvailability(
  materialId: string, quantityAvailable: number, reason: string,
): Promise<MaterialAvailability> {
  return apiFetch<MaterialAvailability>(
    `/materials/${encodeURIComponent(materialId)}/availability`,
    { method: 'PATCH', body: JSON.stringify({ quantity_available: quantityAvailable, reason }) },
  );
}
