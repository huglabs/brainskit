export interface Item {
  sku: string;
  quantity: number;
}

export class Warehouse {
  private items: Map<string, Item> = new Map();

  add(item: Item): void {
    this.items.set(item.sku, item);
  }

  count(sku: string): number {
    const found = this.items.get(sku);
    return found ? found.quantity : 0;
  }
}

export function restock(warehouse: Warehouse, item: Item): number {
  warehouse.add(item);
  return warehouse.count(item.sku);
}
