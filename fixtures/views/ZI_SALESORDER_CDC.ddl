// Valid multi-join view with an explicit CDC mapping.
// Shape follows the verified working example in SAP's TechEd DA281 repository.
@EndUserText.label: 'Sales order extraction with CDC'
@Analytics: { dataCategory: #FACT,
  dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'SNWD_SO',   role: #MAIN,                   viewElement: ['SalesOrderGuid'], tableElement: ['NODE_KEY']},
        {table: 'SNWD_SO_I', role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['ItemGuid'],       tableElement: ['NODE_KEY']},
        {table: 'SNWD_PD',   role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['ProductNodeGuid'], tableElement: ['NODE_KEY']}
      ] } } }
define view entity ZI_SALESORDER_CDC
  as select from snwd_so as SalesOrder
    left outer to one join snwd_so_i as Item    on  Item.parent_key  = SalesOrder.node_key
    left outer to one join snwd_pd   as Product on  Product.node_key = Item.product_guid
{
  key SalesOrder.node_key      as SalesOrderGuid,
      SalesOrder.so_id         as SalesOrderId,
      SalesOrder.gross_amount  as GrossAmount,
      SalesOrder.currency_code as CurrencyCode,
      Item.node_key            as ItemGuid,
      Item.parent_key          as ItemParentGuid,
      Item.product_guid        as ProductGuid,
      Product.node_key         as ProductNodeGuid,
      Product.product_id       as ProductId
}
