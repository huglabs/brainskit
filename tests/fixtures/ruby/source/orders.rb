require_relative "inventory"

class Order
  def initialize(product)
    @product = product
  end

  def total
    @product.price
  end

  def discounted_total(fraction)
    @product.discounted(fraction).price
  end
end

def build_default_order
  Order.new(Catalogue.default)
end
