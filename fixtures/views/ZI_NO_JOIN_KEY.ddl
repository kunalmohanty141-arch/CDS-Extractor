// R-15 — neither side of the join condition is exposed.
//
// The real defect, as opposed to ZI_MISSING_FK where one side is exposed and
// that is enough. Here the projection carries neither Item.parent_key nor
// SalesOrder.node_key, so the join key value is absent from the output
// entirely and no CDC mapping can work out which output row a change to
// SNWD_SO_I belongs to.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'SNWD_SO',   role: #MAIN,                   viewElement: ['ItemGuid'], tableElement: ['NODE_KEY']},
        {table: 'SNWD_SO_I', role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['ItemGuid'], tableElement: ['NODE_KEY']}
      ] } } }
define view entity ZI_NO_JOIN_KEY
  as select from snwd_so as SalesOrder
    left outer to one join snwd_so_i as Item on Item.parent_key = SalesOrder.node_key
{
  key Item.node_key           as ItemGuid,
      SalesOrder.gross_amount as GrossAmount,
      Item.so_item_pos        as ItemPosition
}
