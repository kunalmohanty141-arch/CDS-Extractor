// R-15 — one side of the join condition is exposed, and that is enough.
//
// The ON condition is Item.parent_key = SalesOrder.node_key. parent_key is not
// in the projection, but node_key is, as SalesOrderGuid — and after the join
// the two hold the same value in every row, so the key CDC needs is present.
//
// This fixture used to expect a violation, from a strict reading requiring
// both sides. Measured against the 887 views S/4HANA ships with CDC delta
// declared, that reading flagged 117 of the 123 that have joins. See
// ZI_NO_JOIN_KEY for the case that is genuinely broken.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'SNWD_SO',   role: #MAIN,                   viewElement: ['SalesOrderGuid'], tableElement: ['NODE_KEY']},
        {table: 'SNWD_SO_I', role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['ItemGuid'],       tableElement: ['NODE_KEY']}
      ] } } }
define view entity ZI_MISSING_FK
  as select from snwd_so as SalesOrder
    left outer to one join snwd_so_i as Item on Item.parent_key = SalesOrder.node_key
{
  key SalesOrder.node_key     as SalesOrderGuid,
      SalesOrder.gross_amount as GrossAmount,
      Item.node_key           as ItemGuid,
      Item.so_item_pos        as ItemPosition
}
