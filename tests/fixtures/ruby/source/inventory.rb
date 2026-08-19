# The definitions `orders.rb` requires, so this fixture is cross-file too.

class Product
  attr_reader :sku, :price

  def initialize(sku, price)
    @sku = sku
    @price = price
  end

  def discounted(fraction)
    Product.new(@sku, (@price * (1 - fraction)).round)
  end
end

module Catalogue
  def self.default
    Product.new("SKU-1", 1000)
  end
end
